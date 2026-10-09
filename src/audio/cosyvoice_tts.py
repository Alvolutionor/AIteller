"""CosyVoice 2 TTS adapter — talks to a local FastAPI server exposed by the
FunAudioLLM/CosyVoice Docker container on port 50000.

The container downloads iic/CosyVoice2-0.5B on first run and serves endpoints:
  POST /inference_sft           — built-in speaker (spk_id + tts_text)
  POST /inference_zero_shot     — clone from reference audio
  POST /inference_cross_lingual
  POST /inference_instruct

Responses stream raw 16-bit PCM at 22050 Hz; we wrap as WAV then convert to
MP3 via ffmpeg so the downstream pipeline (video compose) sees the same
format edge_tts produced.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:50000"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_SPEAKERS = {
    # CosyVoice2-0.5B ships these built-in speakers
    "zh": "中文男",
    "en": "英文男",
}


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM into a minimal WAV container."""
    n_samples = len(pcm_bytes) // 2
    byte_rate = sample_rate * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_bytes)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm_bytes))
    return header + pcm_bytes


def _wav_to_mp3(wav_bytes: bytes, out_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to convert CosyVoice WAV to MP3")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(tmp_path), "-codec:a", "libmp3lame",
             "-b:a", "192k", str(out_path)],
            check=True, capture_output=True, timeout=120,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    return out_path


class CosyVoiceTTSEngine:
    """Drop-in replacement for Edge TTSEngine — same `synthesize()` signature."""

    def __init__(self, config: dict):
        audio_cfg = config.get("audio", {})
        cv_cfg = audio_cfg.get("cosyvoice", {})
        self.endpoint = cv_cfg.get("endpoint", DEFAULT_ENDPOINT).rstrip("/")
        self.sample_rate = int(cv_cfg.get("sample_rate", DEFAULT_SAMPLE_RATE))
        self.mode = cv_cfg.get("mode", "sft")  # sft | cross_lingual | instruct
        speakers = cv_cfg.get("speakers", {})
        self.speakers = {
            "zh": speakers.get("zh", DEFAULT_SPEAKERS["zh"]),
            "en": speakers.get("en", DEFAULT_SPEAKERS["en"]),
        }
        self.request_timeout = float(cv_cfg.get("request_timeout_sec", 600))
        self.max_chunk_chars = int(cv_cfg.get("max_chunk_chars", 400))
        self.instruct_text = cv_cfg.get(
            "instruct_text",
            "用平稳、清晰的技术播报语气朗读，节奏略慢，专业冷静，不带情绪起伏。",
        )

    async def _synth_one(self, client: httpx.AsyncClient, text: str, spk_id: str) -> bytes:
        """Call CosyVoice server, return WAV bytes for one chunk."""
        if self.mode == "instruct":
            url = f"{self.endpoint}/inference_instruct"
            data = {"tts_text": text, "spk_id": spk_id, "instruct_text": self.instruct_text}
        elif self.mode == "cross_lingual":
            url = f"{self.endpoint}/inference_cross_lingual"
            data = {"tts_text": text}
        else:
            url = f"{self.endpoint}/inference_sft"
            data = {"tts_text": text, "spk_id": spk_id}

        resp = await client.post(url, data=data, timeout=self.request_timeout)
        resp.raise_for_status()
        pcm = resp.content
        if not pcm:
            raise RuntimeError(f"Empty audio from CosyVoice for: {text[:50]!r}")
        return _pcm_to_wav(pcm, self.sample_rate)

    def _split(self, text: str) -> list[str]:
        """Split by paragraph; further split any paragraph over max_chunk_chars
        at Chinese/English sentence punctuation."""
        out: list[str] = []
        for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
            if len(para) <= self.max_chunk_chars:
                out.append(para)
                continue
            buf = ""
            for ch in para:
                buf += ch
                if len(buf) >= self.max_chunk_chars and ch in "。！？.!?；;\n":
                    out.append(buf.strip()); buf = ""
            if buf.strip():
                out.append(buf.strip())
        return out

    async def synthesize(self, script: str, lang: str, output_path: Path) -> Path:
        from src.audio.tts import _sanitize_for_tts

        if not script.strip():
            raise ValueError("Empty script, nothing to synthesize")

        clean = _sanitize_for_tts(script)
        if not clean:
            raise ValueError("Script became empty after sanitization")

        spk = self.speakers.get(lang, self.speakers["en"])
        chunks = self._split(clean)
        logger.info("CosyVoice: %d chunks, endpoint=%s, spk=%s, mode=%s",
                    len(chunks), self.endpoint, spk, self.mode)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        pcm_all = bytearray()
        async with httpx.AsyncClient() as client:
            for i, chunk in enumerate(chunks):
                try:
                    wav = await self._synth_one(client, chunk, spk)
                    pcm_all.extend(wav[44:])  # drop per-chunk WAV header; we rewrap once
                    silence_samples = int(self.sample_rate * 0.3)
                    pcm_all.extend(b"\x00\x00" * silence_samples)
                    logger.info("CosyVoice chunk %d/%d ok (%d chars)",
                                i + 1, len(chunks), len(chunk))
                except Exception as e:
                    logger.warning("CosyVoice chunk %d/%d failed: %s",
                                   i + 1, len(chunks), e)

        if not pcm_all:
            raise RuntimeError("CosyVoice produced no audio — all chunks failed")

        wav_bytes = _pcm_to_wav(bytes(pcm_all), self.sample_rate)
        await asyncio.to_thread(_wav_to_mp3, wav_bytes, output_path)
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("CosyVoice complete: %s (%.1f MB)", output_path.name, size_mb)
        return output_path
