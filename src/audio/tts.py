"""Edge TTS wrapper — convert text script to MP3 audio."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_VOICES = {
    "zh": "zh-CN-YunxiNeural",
    "en": "en-US-GuyNeural",
}


# ── TTS sanitization ────────────────────────────────────────────────
# Edge TTS literally reads markdown punctuation: `**bold**` becomes
# "星星 bold 星星" in Chinese. Strip all markdown + stage directions
# before synthesis so the script is pure spoken text.

_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_BOLD = re.compile(r"\*\*([\s\S]+?)\*\*")
_RE_UNDERSCORE_BOLD = re.compile(r"__([\s\S]+?)__")
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_RE_BULLET = re.compile(r"^[ \t]*[-*+•]\s+", re.MULTILINE)
_RE_NUMBERED = re.compile(r"^[ \t]*\d+[.)]\s+", re.MULTILINE)
_RE_QUOTE = re.compile(r"^[ \t]*>\s?", re.MULTILINE)
_RE_HR = re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.MULTILINE)

# Stage directions: either a full line wrapped in (...) / （...）, or an
# inline parenthetical containing audio cue keywords.
_RE_STAGE_LINE = re.compile(r"^[ \t]*[（(][^)）]*[)）][ \t]*$", re.MULTILINE)
_STAGE_KEYWORDS = (
    "音乐|渐弱|渐强|淡入|淡出|停顿|背景音|BGM|过渡|片头|片尾|效果音|"
    r"music|fade|pause|intro|outro|sound|sfx|bgm|cue|ambient|transition"
)
_RE_STAGE_INLINE = re.compile(
    rf"[（(][^（()）]{{0,80}}?(?:{_STAGE_KEYWORDS})[^（()）]{{0,80}}?[)）]",
    re.IGNORECASE,
)

_RE_STRAY_STARS = re.compile(r"\*+")
_RE_MULTI_BLANK = re.compile(r"\n{3,}")
_RE_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def _sanitize_for_tts(script: str) -> str:
    """Strip markdown + stage directions so TTS reads clean prose."""
    s = script
    s = _RE_CODE_BLOCK.sub("", s)
    s = _RE_IMAGE.sub("", s)
    s = _RE_LINK.sub(r"\1", s)
    s = _RE_BOLD.sub(r"\1", s)
    s = _RE_UNDERSCORE_BOLD.sub(r"\1", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    s = _RE_HEADER.sub("", s)
    s = _RE_BULLET.sub("", s)
    s = _RE_NUMBERED.sub("", s)
    s = _RE_QUOTE.sub("", s)
    s = _RE_HR.sub("", s)
    s = _RE_STAGE_LINE.sub("", s)
    s = _RE_STAGE_INLINE.sub("", s)
    s = _RE_STRAY_STARS.sub("", s)
    s = _RE_TRAILING_WS.sub("", s)
    s = _RE_MULTI_BLANK.sub("\n\n", s)
    return s.strip()


def make_tts_engine(config: dict):
    """Factory: dispatch to engine based on config.audio.engine (edge | cosyvoice)."""
    engine = (config.get("audio", {}).get("engine") or "edge").lower()
    if engine in ("cosyvoice", "cosyvoice2"):
        from src.audio.cosyvoice_tts import CosyVoiceTTSEngine
        return CosyVoiceTTSEngine(config)
    return TTSEngine(config)


class TTSEngine:
    def __init__(self, config: dict):
        audio_cfg = config.get("audio", {})
        voices_cfg = audio_cfg.get("voices", {})
        self.voices = {
            "zh": voices_cfg.get("zh", DEFAULT_VOICES["zh"]),
            "en": voices_cfg.get("en", DEFAULT_VOICES["en"]),
        }
        # Playback speed: "+0%" normal, "+20%" faster, "-10%" slower
        self.rate = audio_cfg.get("tts_rate", "+0%")

    async def synthesize(self, script: str, lang: str, output_path: Path) -> Path:
        """Convert script text to MP3 via Edge TTS.

        Splits by paragraph to avoid timeouts on long text,
        then concatenates raw MP3 bytes (frames are independently decodable).
        """
        if not script.strip():
            raise ValueError("Empty script, nothing to synthesize")

        clean = _sanitize_for_tts(script)
        if not clean:
            raise ValueError("Script became empty after sanitization")

        voice = self.voices.get(lang, DEFAULT_VOICES.get(lang, "en-US-GuyNeural"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Split into paragraphs (double newline)
        chunks = [c.strip() for c in clean.split("\n\n") if c.strip()]
        if not chunks:
            chunks = [script.strip()]

        logger.info("TTS: %d chunks, voice=%s, output=%s", len(chunks), voice, output_path)

        mp3_bytes = bytearray()
        for i, chunk in enumerate(chunks):
            try:
                communicate = edge_tts.Communicate(chunk, voice, rate=self.rate)
                chunk_bytes = bytearray()
                async for msg in communicate.stream():
                    if msg["type"] == "audio":
                        chunk_bytes.extend(msg["data"])
                mp3_bytes.extend(chunk_bytes)
                # Add a small silence gap between paragraphs (~300ms of silence)
                # MP3 silence frame (128kbps, ~26ms per frame, ~12 frames ≈ 300ms)
                mp3_bytes.extend(b'\xff\xfb\x90\x00' + b'\x00' * 413 * 12)
            except Exception as e:
                logger.warning("TTS chunk %d/%d failed, skipping: %s", i + 1, len(chunks), e)
                continue

        if not mp3_bytes:
            raise RuntimeError("TTS produced no audio — all chunks failed")

        output_path.write_bytes(bytes(mp3_bytes))
        size_mb = len(mp3_bytes) / (1024 * 1024)
        logger.info("TTS complete: %s (%.1f MB)", output_path.name, size_mb)
        return output_path
