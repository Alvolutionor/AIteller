"""Slideshow MP4 composer — per-item slide rendered as PNG, timed to each
segment's TTS audio, concatenated via ffmpeg into a single MP4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

import edge_tts

from src.audio.segmented_script import Segment
from src.audio.tts import _sanitize_for_tts, DEFAULT_VOICES

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTION = (1920, 1080)


# ── Slide rendering ────────────────────────────────────────────────

def _find_font(lang: str, bold: bool) -> str | None:
    if lang == "zh":
        candidates = (
            ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"]
            if bold else
            ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]
        )
    else:
        candidates = (
            ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/ariblk.ttf"]
            if bold else
            ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
        )
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Wrap text into lines that fit max_width in pixels.
    Works for CJK (char-level) and Latin (word-level).
    """
    if not text:
        return []
    lines: list[str] = []
    current = ""
    # Decide if CJK-heavy by counting CJK chars
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    if cjk > len(text) // 3:
        for ch in text:
            test = current + ch
            w = draw.textbbox((0, 0), test, font=font)[2]
            if w > max_width and current:
                lines.append(current)
                current = ch
            else:
                current = test
        if current:
            lines.append(current)
    else:
        for word in text.split():
            test = f"{current} {word}".strip()
            w = draw.textbbox((0, 0), test, font=font)[2]
            if w > max_width and current:
                lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)
    return lines


def _draw_gradient(draw, w, h, top, bot):
    for y in range(h):
        r = y / h
        draw.line([(0, y), (w, y)],
                  fill=(int(top[0] * (1 - r) + bot[0] * r),
                        int(top[1] * (1 - r) + bot[1] * r),
                        int(top[2] * (1 - r) + bot[2] * r)))


def _draw_dot_grid(draw, w, h, color=(255, 255, 255, 16), spacing=60, radius=1):
    for x in range(0, w, spacing):
        for y in range(0, h, spacing):
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)


def _draw_glow_blobs(img, blobs):
    from PIL import Image, ImageDraw, ImageFilter
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for cx, cy, r, color, alpha in blobs:
        od.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=80))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def _draw_circuit_lines(draw, w, h, color=(99, 102, 241, 60), count=10):
    import math, random
    rng = random.Random(42)
    for _ in range(count):
        y = rng.randint(50, h - 80)
        x0 = rng.randint(0, w // 3)
        x1 = rng.randint(w // 2, w)
        draw.line([(x0, y), (x1, y)], fill=color, width=1)
        nx = rng.randint(x0 + 20, x1 - 20)
        draw.ellipse([nx - 3, y - 3, nx + 3, y + 3], fill=color)
        bl = rng.randint(15, 50) * rng.choice([-1, 1])
        draw.line([(nx, y), (nx, y + bl)], fill=color, width=1)


def _draw_hexagons(draw, positions, size=35, color=(99, 102, 241, 60), width=1):
    import math
    for cx, cy in positions:
        pts = []
        for i in range(6):
            a = math.radians(60 * i - 30)
            pts.append((cx + size * math.cos(a), cy + size * math.sin(a)))
        draw.polygon(pts, outline=color)


def render_slide(segment: Segment, resolution: tuple[int, int],
                 week_start: str, week_end: str, lang: str,
                 out_path: Path) -> Path:
    """Render a single slide PNG for the given segment."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = resolution
    img = Image.new("RGB", (w, h), (15, 14, 34))  # deep navy
    draw = ImageDraw.Draw(img)

    # ── Background gradient (top → bottom) ──
    top, bot = (18, 18, 46), (10, 22, 48)
    _draw_gradient(draw, w, h, top, bot)

    # Accent top bar
    draw.rectangle([(0, 0), (w, 6)], fill=(236, 72, 153))
    # Decorative side bar
    draw.rectangle([(0, 0), (8, h)], fill=(99, 102, 241))

    # Fonts
    bold_path = _find_font(lang, bold=True)
    reg_path = _find_font(lang, bold=False)

    def F(path, size):
        try:
            return ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    padding_x = int(w * 0.06)
    content_w = w - padding_x * 2

    # ── Content by kind ──
    if segment.kind == "intro":
        # Rich cover with Bilibili-style clickbait: big number, teasers, glow
        _draw_glow_blobs(img, [
            (300, 250, 320, (236, 72, 153), 45),    # pink top-left
            (1620, 780, 300, (59, 130, 246), 40),   # blue bottom-right
            (1100, 500, 380, (99, 102, 241), 20),   # indigo center
        ])
        draw = ImageDraw.Draw(img)  # re-acquire after paste
        _draw_dot_grid(draw, w, h, color=(255, 255, 255, 18), spacing=55, radius=1)
        _draw_circuit_lines(draw, w, h, color=(99, 102, 241, 55), count=12)
        _draw_hexagons(draw, [
            (150, 150), (1780, 130), (1700, 930),
            (110, 880), (960, 90), (1820, 450),
        ], size=38, color=(99, 102, 241, 60), width=1)

        # Re-paint top accent bar and side bar on top of decorations
        draw.rectangle([(0, 0), (w, 6)], fill=(236, 72, 153))
        draw.rectangle([(0, 0), (8, h)], fill=(99, 102, 241))

        # Main title: big with shadow
        f_title = F(bold_path, 150)
        title_x, title_y = int(w * 0.06), int(h * 0.09)
        draw.text((title_x + 4, title_y + 4), segment.title, fill=(0, 0, 0), font=f_title)
        draw.text((title_x, title_y), segment.title, fill=(255, 255, 255), font=f_title)

        # Gradient underline
        title_bbox = draw.textbbox((title_x, title_y), segment.title, font=f_title)
        ul_y = title_bbox[3] + 14
        ul_w = title_bbox[2] - title_bbox[0]
        for i in range(ul_w):
            r = i / ul_w
            rc = (int(236 * (1 - r) + 99 * r),
                  int(72 * (1 - r) + 102 * r),
                  int(153 * (1 - r) + 241 * r))
            draw.line([(title_x + i, ul_y), (title_x + i, ul_y + 5)], fill=rc)

        # Subtitle (date range)
        f_sub = F(reg_path, 48)
        draw.text((title_x, ul_y + 22), segment.subtitle,
                  fill=(203, 213, 225), font=f_sub)

        # GIANT ITEM COUNT badge (right side, B-station hook)
        count_str = str(segment.body or segment.meta.get("item_count", ""))
        f_count = F(bold_path, 360)
        count_bbox = draw.textbbox((0, 0), count_str, font=f_count)
        count_w = count_bbox[2] - count_bbox[0]
        count_h_ = count_bbox[3] - count_bbox[1]
        count_x = w - padding_x - count_w
        count_y = int(h * 0.08)
        # Drop shadow
        draw.text((count_x + 6, count_y + 6), count_str, fill=(0, 0, 0), font=f_count)
        # Main (hot pink → amber gradient-ish via two layers)
        draw.text((count_x, count_y), count_str, fill=(236, 72, 153), font=f_count)
        # Label under number
        f_count_label = F(bold_path, 48)
        label_text = "条实战干货" if lang == "zh" else "curated items"
        lb = draw.textbbox((0, 0), label_text, font=f_count_label)
        lw = lb[2] - lb[0]
        draw.text((count_x + (count_w - lw) // 2, count_y + count_h_ + 20),
                  label_text, fill=(255, 255, 255), font=f_count_label)

        # Clickbait hook line
        hook = segment.meta.get("hook",
                                "本期爆点 · TOP 3" if lang == "zh" else "TOP 3 HIGHLIGHTS")
        f_hook_label = F(bold_path, 48)
        hook_y = int(h * 0.52)
        # Hook pill background
        hb = draw.textbbox((0, 0), hook, font=f_hook_label)
        hook_w = hb[2] - hb[0]
        pill_pad = 24
        pill = (title_x - pill_pad, hook_y - pill_pad + 6,
                title_x + hook_w + pill_pad, hook_y + f_hook_label.size + pill_pad - 6)
        draw.rounded_rectangle(pill, radius=18, fill=(236, 72, 153), outline=None)
        draw.text((title_x, hook_y), hook, fill=(255, 255, 255), font=f_hook_label)

        # Top 3 teasers (clickbait)
        teasers = segment.meta.get("teasers", [])
        f_teaser = F(bold_path, 44)
        f_teaser_num = F(bold_path, 40)
        hl_y = hook_y + f_hook_label.size + 55
        accent = (236, 72, 153)
        for i, teaser in enumerate(teasers[:3]):
            line_y = hl_y + i * 80
            # Number badge
            badge_r = 26
            badge_cx = title_x + badge_r
            badge_cy = line_y + badge_r + 2
            draw.ellipse([badge_cx - badge_r, badge_cy - badge_r,
                          badge_cx + badge_r, badge_cy + badge_r],
                         fill=accent)
            num_str = str(i + 1)
            nb = draw.textbbox((0, 0), num_str, font=f_teaser_num)
            nw, nh = nb[2] - nb[0], nb[3] - nb[1]
            draw.text((badge_cx - nw // 2, badge_cy - nh // 2 - 4),
                      num_str, fill=(255, 255, 255), font=f_teaser_num)
            # Teaser text (truncate to fit)
            teaser_text = teaser
            max_chars = 28 if lang == "zh" else 54
            if len(teaser_text) > max_chars:
                teaser_text = teaser_text[:max_chars - 1] + "…"
            draw.text((title_x + badge_r * 2 + 24, line_y + 8),
                      teaser_text, fill=(226, 232, 240), font=f_teaser)

        # Bottom tagline / CTA (Bilibili-flavored)
        f_tag = F(bold_path, 34)
        tagline = (
            "干货不注水 · 一期看完 · 点赞关注不迷路"
            if lang == "zh" else
            "No fluff · All in one shot · Subscribe for more"
        )
        tag_bbox = draw.textbbox((0, 0), tagline, font=f_tag)
        tag_w = tag_bbox[2] - tag_bbox[0]
        draw.text(((w - tag_w) // 2, h - 90), tagline,
                  fill=(236, 72, 153), font=f_tag)

        # Bottom bar
        draw.rectangle([(0, h - 44), (w, h)], fill=(10, 10, 30))
        draw.rectangle([(0, h - 44), (w, h - 42)], fill=(99, 102, 241))

    elif segment.kind == "tier":
        f_label = F(reg_path, 40)
        f_title = F(bold_path, 180)
        f_sub = F(reg_path, 56)
        label = "章节" if lang == "zh" else "SECTION"
        y = int(h * 0.30)
        draw.text((padding_x, y), label, fill=(148, 163, 184), font=f_label)
        y += f_label.size + 20
        draw.text((padding_x, y), segment.title, fill=(255, 255, 255), font=f_title)
        y += f_title.size + 30
        draw.text((padding_x, y), segment.subtitle, fill=(99, 102, 241), font=f_sub)

    elif segment.kind == "outro":
        f_title = F(bold_path, 150)
        f_sub = F(reg_path, 52)
        y = int(h * 0.35)
        draw.text((padding_x, y), segment.title, fill=(255, 255, 255), font=f_title)
        y += f_title.size + 40
        draw.text((padding_x, y), segment.subtitle, fill=(148, 163, 184), font=f_sub)

    else:  # item
        f_counter = F(bold_path, 36)
        f_source = F(bold_path, 42)

        # Top row: counter + source pill (fixed above title area)
        top_y = int(h * 0.05)
        if segment.counter:
            cur, tot = segment.counter
            draw.text((padding_x, top_y), f"{cur:03d} / {tot:03d}",
                      fill=(148, 163, 184), font=f_counter)

        source_text = segment.source or segment.subtitle
        src_bbox = draw.textbbox((0, 0), source_text, font=f_source)
        src_w = src_bbox[2] - src_bbox[0]
        src_x = w - padding_x - src_w
        pill_pad = 18
        pill_xy = (src_x - pill_pad, top_y - pill_pad + 4,
                   src_x + src_w + pill_pad, top_y + f_source.size + pill_pad - 4)
        draw.rounded_rectangle(pill_xy, radius=14,
                               fill=(30, 30, 70), outline=(99, 102, 241), width=2)
        draw.text((src_x, top_y), source_text,
                  fill=(236, 72, 153), font=f_source)

        # Layout: content area between top bar and footer
        content_top = int(h * 0.16)
        content_bottom = h - int(h * 0.07)
        content_h = content_bottom - content_top
        # Budget: ~30% to title, 70% to body (adjust dynamically)
        title_budget_ratio = 0.32

        # Pick title font size that fits within 3 lines AND title_budget of area
        title_text = segment.title or ""
        title_size = 72
        title_line_h = lambda s: int(s * 1.25)
        title_lines: list[str] = []
        for candidate in (78, 68, 60, 54, 48, 44, 40):
            f_try = F(bold_path, candidate)
            lines = _wrap_text(draw, title_text, f_try, content_w)
            if not lines:
                lines = [title_text]
            needed = len(lines) * title_line_h(candidate)
            if needed <= int(content_h * title_budget_ratio) and len(lines) <= 4:
                title_size = candidate
                title_lines = lines
                break
        else:
            f_try = F(bold_path, 40)
            title_lines = _wrap_text(draw, title_text, f_try, content_w)[:4]
            title_size = 40
        f_title = F(bold_path, title_size)

        title_y = content_top
        for line in title_lines:
            draw.text((padding_x, title_y), line, fill=(255, 255, 255), font=f_title)
            title_y += title_line_h(title_size)

        # Body area — pick font size that fits entire summary (no truncation)
        body_y0 = title_y + 30
        body_h = content_bottom - body_y0
        body_text = segment.body or ""
        body_size = 40
        body_line_h = lambda s: int(s * 1.5)
        body_lines: list[str] = []
        for candidate in (46, 42, 38, 34, 30, 28, 26, 24):
            f_try = F(reg_path, candidate)
            lines = _wrap_text(draw, body_text, f_try, content_w)
            if not lines:
                break
            needed = len(lines) * body_line_h(candidate)
            if needed <= body_h:
                body_size = candidate
                body_lines = lines
                break
        else:
            # Text is so long even the smallest font doesn't fit: use smallest and allow clipping via line cap
            f_try = F(reg_path, 22)
            body_size = 22
            body_lines = _wrap_text(draw, body_text, f_try, content_w)
            max_lines = max(1, body_h // body_line_h(body_size))
            if len(body_lines) > max_lines:
                body_lines = body_lines[:max_lines]
                if body_lines:
                    body_lines[-1] = body_lines[-1][:-2] + "…"
        f_body = F(reg_path, body_size)

        by = body_y0
        for line in body_lines:
            draw.text((padding_x, by), line, fill=(226, 232, 240), font=f_body)
            by += body_line_h(body_size)

        # Footer
        f_foot = F(reg_path, 28)
        foot_text = f"AI 工程周报 · {week_start} ~ {week_end}" if lang == "zh" \
                    else f"AI Engineering Weekly · {week_start} ~ {week_end}"
        draw.text((padding_x, h - 52), foot_text,
                  fill=(100, 116, 139), font=f_foot)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path


# ── TTS per-segment ────────────────────────────────────────────────

async def synth_segment(segment: Segment, voice: str, rate: str,
                        out_path: Path, max_retries: int = 6) -> tuple[Path, float]:
    """Synthesize one segment via edge-tts → mp3. Return (path, duration_sec).

    Edge TTS (Microsoft's Bing speech endpoint) intermittently 503s the
    WebSocket handshake when a long run fires many segments back-to-back —
    it rate-limits per IP. A single failure used to abort the whole 105-segment
    job; now each segment retries with exponential backoff + jitter, which both
    rides through the throttle and naturally spaces out requests. Combined with
    the segment-level resume cache in build_slideshow, even a hard failure is
    cheaply recoverable on re-run.
    """
    text = _sanitize_for_tts(segment.text)
    if not text:
        raise ValueError(f"Empty text after sanitize in segment: {segment.kind}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            mp3_bytes = bytearray()
            async for msg in communicate.stream():
                if msg["type"] == "audio":
                    mp3_bytes.extend(msg["data"])
            if not mp3_bytes:
                raise RuntimeError("edge-tts returned no audio data")
            out_path.write_bytes(bytes(mp3_bytes))
            return out_path, probe_duration(out_path)
        except Exception as exc:  # WSServerHandshakeError, ClientError, empty audio, …
            last_exc = exc
            if attempt < max_retries - 1:
                wait = min(2 ** attempt * 3, 60) + random.uniform(0, 2)
                logger.warning(
                    "synth_segment (%s): edge-tts failed attempt %d/%d (%s) — "
                    "retrying in %.0fs", segment.kind, attempt + 1, max_retries,
                    exc, wait,
                )
                await asyncio.sleep(wait)

    raise RuntimeError(
        f"edge-tts failed after {max_retries} attempts for {segment.kind}: {last_exc}"
    ) from last_exc


def probe_duration(mp3_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required")
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(r.stdout.strip())


# ── ffmpeg concat ──────────────────────────────────────────────────

def _check_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg required for slideshow")
    return p


def build_segment_video(slide: Path, audio: Path, duration: float,
                        resolution: tuple[int, int], out_path: Path,
                        min_duration: float = 0.0) -> Path:
    """Create a per-segment mp4 of the still image with audio.

    If min_duration exceeds audio length, audio is padded with silence so the
    slide stays long enough to be read.
    """
    ffmpeg = _check_ffmpeg()
    w, h = resolution
    target = max(duration, min_duration)
    cmd = [
        ffmpeg, "-y",
        "-loop", "1", "-i", str(slide),
        "-i", str(audio),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-vf", f"scale={w}:{h}",
        "-af", "apad",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-t", f"{target:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg segment build failed: {r.stderr[-500:]}")
    return out_path


def concat_videos(segment_videos: Sequence[Path], out_path: Path) -> Path:
    """Concatenate per-segment MP4s via the concat demuxer."""
    ffmpeg = _check_ffmpeg()
    list_file = out_path.parent / f"{out_path.stem}.concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segment_videos),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg, "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        # concat copy sometimes fails on slightly mismatched streams — fall back to re-encode
        logger.warning("concat copy failed, retrying with re-encode")
        cmd2 = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=1200)
        if r2.returncode != 0:
            raise RuntimeError(f"ffmpeg concat reencode failed: {r2.stderr[-500:]}")
    list_file.unlink(missing_ok=True)
    return out_path


# ── High-level API ─────────────────────────────────────────────────

async def build_slideshow(segments: list[Segment],
                          voice: str, rate: str,
                          resolution: tuple[int, int],
                          week_start: str, week_end: str, lang: str,
                          work_dir: Path, out_path: Path,
                          cover_out_path: Path | None = None) -> Path:
    """Full pipeline: render slides + synth audio + concat to MP4.

    If `cover_out_path` is given, the first intro slide PNG is also copied
    there for use as a standalone thumbnail (e.g. Bilibili upload cover).
    """
    import shutil as _shutil

    work_dir.mkdir(parents=True, exist_ok=True)
    segs_dir = work_dir / "segments"
    segs_dir.mkdir(exist_ok=True)

    logger.info("Slideshow: %d segments, voice=%s, rate=%s",
                len(segments), voice, rate)

    # Per-kind minimum on-screen duration (seconds). Pads short TTS with silence
    # so viewers have time to read longer slides.
    min_duration_by_kind = {
        "intro": 8.0,   # cover needs to linger — B-station hook style
        "tier": 3.0,
        "item": 6.0,    # full summary needs reading time
        "outro": 4.0,
    }

    segment_videos: list[Path] = []
    resumed = 0
    for i, seg in enumerate(segments):
        slide_path = segs_dir / f"slide_{i:03d}.png"
        audio_path = segs_dir / f"audio_{i:03d}.mp3"
        video_path = segs_dir / f"seg_{i:03d}.mp4"

        # Resume: if a previous run already produced this segment's
        # final .mp4 and the inputs (slide + audio) still exist, reuse
        # it. Saves 30+ minutes when Edge TTS DNS/network blips abort
        # mid-build (which is common from China).
        if (video_path.exists() and video_path.stat().st_size > 1000
                and slide_path.exists() and audio_path.exists()
                and audio_path.stat().st_size > 100):
            segment_videos.append(video_path)
            resumed += 1
            if (i + 1) % 10 == 0 or i == len(segments) - 1:
                logger.info("Slideshow: %d/%d segments done (%d reused)",
                            i + 1, len(segments), resumed)
            continue

        render_slide(seg, resolution, week_start, week_end, lang, slide_path)

        # Export first intro slide as standalone cover thumbnail
        if cover_out_path is not None and seg.kind == "intro":
            cover_out_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(slide_path, cover_out_path)
            logger.info("Cover thumbnail: %s", cover_out_path)

        _, duration = await synth_segment(seg, voice, rate, audio_path)
        min_d = min_duration_by_kind.get(seg.kind, 0.0)
        build_segment_video(slide_path, audio_path, duration, resolution,
                            video_path, min_duration=min_d)
        segment_videos.append(video_path)

        if (i + 1) % 10 == 0 or i == len(segments) - 1:
            logger.info("Slideshow: %d/%d segments done (%d reused)",
                        i + 1, len(segments), resumed)

    concat_videos(segment_videos, out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info("Slideshow MP4: %s (%.1f MB)", out_path.name, size_mb)
    return out_path
