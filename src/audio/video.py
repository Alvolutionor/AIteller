"""Video generation — cover image (Pillow) + ffmpeg composition."""

from __future__ import annotations

import json
import logging
import math
import random
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

RESOLUTION = (1920, 1080)


def _check_ffmpeg() -> str:
    """Return ffmpeg path or raise clear error."""
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "ffmpeg is required for video generation but was not found on PATH.\n"
            "Install from https://ffmpeg.org/ and ensure it's on your system PATH."
        )
    return path


# ── Font helpers ──────────────────────────────────────────────────────

def _find_bold_font(lang: str) -> str | None:
    import os
    if lang == "zh":
        candidates = [
            "C:/Windows/Fonts/msyhbd.ttc",     # Microsoft YaHei Bold
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/msyh.ttc",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/impact.ttf",
            "C:/Windows/Fonts/ariblk.ttf",      # Arial Black
            "C:/Windows/Fonts/segoeuib.ttf",
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _find_regular_font(lang: str) -> str | None:
    import os
    if lang == "zh":
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


# ── Drawing helpers ───────────────────────────────────────────────────

def _draw_gradient(draw, w, h, top, bottom):
    """Vertical linear gradient."""
    for y in range(h):
        ratio = y / h
        r = int(top[0] * (1 - ratio) + bottom[0] * ratio)
        g = int(top[1] * (1 - ratio) + bottom[1] * ratio)
        b = int(top[2] * (1 - ratio) + bottom[2] * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def _draw_dot_grid(draw, w, h, color, spacing=60, radius=1):
    """Subtle dot grid for tech feel."""
    for x in range(0, w, spacing):
        for y in range(0, h, spacing):
            draw.ellipse(
                [x - radius, y - radius, x + radius, y + radius],
                fill=color,
            )


def _draw_glow_circles(img, circles: list[tuple]):
    """Draw soft glowing circles via alpha compositing."""
    from PIL import Image, ImageDraw, ImageFilter

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for cx, cy, r, color, alpha in circles:
        od.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(*color, alpha),
        )
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=80))
    img.paste(Image.alpha_composite(
        img.convert("RGBA"), overlay
    ).convert("RGB"))


def _draw_circuit_lines(draw, w, h, color, count=8):
    """Decorative horizontal circuit-board-like lines."""
    rng = random.Random(42)  # deterministic
    for _ in range(count):
        y = rng.randint(50, h - 50)
        x_start = rng.randint(0, w // 3)
        x_end = rng.randint(w // 2, w)
        draw.line([(x_start, y), (x_end, y)], fill=color, width=1)
        # Node dot at a joint
        node_x = rng.randint(x_start + 20, x_end - 20)
        draw.ellipse(
            [node_x - 3, y - 3, node_x + 3, y + 3],
            fill=color,
        )
        # Short vertical branch
        branch_len = rng.randint(15, 50)
        direction = rng.choice([-1, 1])
        draw.line(
            [(node_x, y), (node_x, y + branch_len * direction)],
            fill=color, width=1,
        )


def _draw_hexagons(draw, positions, size, color, width=2):
    """Draw hexagon outlines at given positions."""
    for cx, cy in positions:
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            px = cx + size * math.cos(angle)
            py = cy + size * math.sin(angle)
            points.append((px, py))
        draw.polygon(points, outline=color)


def _draw_rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _extract_top_titles(items: list[dict], lang: str, count: int = 3) -> list[str]:
    """Extract top N item titles for cover display."""
    titles = []
    for item in items[:count * 2]:  # scan a few extra in case of blanks
        if len(titles) >= count:
            break
        if lang == "zh":
            summary_data = item.get("summary", "")
            if isinstance(summary_data, str):
                try:
                    summary_data = json.loads(summary_data)
                except (json.JSONDecodeError, TypeError):
                    summary_data = {}
            title = (summary_data.get("title_zh") or item.get("title", "")) if isinstance(summary_data, dict) else item.get("title", "")
        else:
            title = item.get("title", "")
        if title:
            # Truncate long titles
            if len(title) > 45:
                title = title[:42] + "..."
            titles.append(title)
    return titles


# ── Main class ────────────────────────────────────────────────────────

class VideoMaker:
    def __init__(self, config: dict):
        audio_cfg = config.get("audio", {})
        video_cfg = audio_cfg.get("video", {})
        self.resolution = tuple(video_cfg.get("resolution", RESOLUTION))

    def create_cover(
        self,
        lang: str,
        week_start: str,
        week_end: str,
        item_count: int,
        output_dir: Path,
        items: list[dict] | None = None,
    ) -> Path:
        """Generate an eye-catching cover image PNG."""
        from PIL import Image, ImageDraw, ImageFont

        w, h = self.resolution
        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)

        # ── 1. Background gradient ──
        bg_top = (15, 12, 41)       # deep purple-black
        bg_bottom = (12, 25, 56)    # dark navy
        _draw_gradient(draw, w, h, bg_top, bg_bottom)

        # ── 2. Subtle dot grid overlay ──
        _draw_dot_grid(draw, w, h, color=(255, 255, 255, 20), spacing=50, radius=1)

        # ── 3. Glow circles (soft colored blobs) ──
        _draw_glow_circles(img, [
            (300, 250, 280, (99, 102, 241), 30),     # indigo glow top-left
            (1600, 800, 250, (236, 72, 153), 25),    # pink glow bottom-right
            (1000, 500, 350, (59, 130, 246), 15),     # blue glow center
        ])
        draw = ImageDraw.Draw(img)  # re-acquire after paste

        # ── 4. Circuit lines ──
        _draw_circuit_lines(draw, w, h, color=(99, 102, 241, 40), count=10)

        # ── 5. Decorative hexagons ──
        _draw_hexagons(draw, [
            (150, 150), (1780, 120), (1700, 900),
            (100, 850), (950, 80),
        ], size=35, color=(99, 102, 241, 50), width=1)

        # ── 6. Top accent bar ──
        accent = (236, 72, 153)  # vibrant pink
        draw.rectangle([(0, 0), (w, 5)], fill=accent)

        # ── 7. Load fonts ──
        bold_path = _find_bold_font(lang)
        regular_path = _find_regular_font(lang)
        en_bold_path = _find_bold_font("en")

        def _font(path, size):
            try:
                return ImageFont.truetype(path, size) if path else ImageFont.load_default()
            except Exception:
                return ImageFont.load_default()

        font_title = _font(bold_path, 82)
        font_subtitle = _font(regular_path, 34)
        font_date = _font(regular_path, 40)
        font_badge = _font(en_bold_path or bold_path, 56)
        font_badge_label = _font(regular_path, 20)
        font_headline = _font(regular_path, 28)
        font_headline_num = _font(en_bold_path or bold_path, 28)
        font_watermark = _font(regular_path, 18)

        # ── 8. Main title ──
        title_text = "AI 实践周报" if lang == "zh" else "AI PRACTICE WEEKLY"
        # Shadow
        draw.text((w * 0.08 + 3, h * 0.13 + 3), title_text,
                  fill=(0, 0, 0, 80), font=font_title)
        # Main
        draw.text((w * 0.08, h * 0.13), title_text,
                  fill=(255, 255, 255), font=font_title)

        # ── 9. Accent underline below title ──
        title_bbox = draw.textbbox((w * 0.08, h * 0.13), title_text, font=font_title)
        underline_y = title_bbox[3] + 12
        gradient_bar_w = title_bbox[2] - title_bbox[0]
        # Draw a gradient bar (pink → indigo)
        for i in range(int(gradient_bar_w)):
            ratio = i / gradient_bar_w
            r = int(236 * (1 - ratio) + 99 * ratio)
            g = int(72 * (1 - ratio) + 102 * ratio)
            b = int(153 * (1 - ratio) + 241 * ratio)
            draw.line([(w * 0.08 + i, underline_y),
                       (w * 0.08 + i, underline_y + 4)], fill=(r, g, b))

        # ── 10. Subtitle / tagline ──
        if lang == "zh":
            subtitle = "每周精选 · AI 工程实践社群"
        else:
            subtitle = "Weekly Curated · AI Engineering Community"
        draw.text((w * 0.08, underline_y + 20), subtitle,
                  fill=(148, 163, 184), font=font_subtitle)

        # ── 11. Date range (right-aligned, top area) ──
        date_text = f"{week_start}  →  {week_end}"
        date_bbox = draw.textbbox((0, 0), date_text, font=font_date)
        date_w = date_bbox[2] - date_bbox[0]
        draw.text((w - date_w - w * 0.08, h * 0.15), date_text,
                  fill=(203, 213, 225), font=font_date)

        # ── 12. Stats badge (item count) ──
        badge_x = w - 300
        badge_y = h * 0.35
        # Badge background
        _draw_rounded_rect(draw,
                           (badge_x, badge_y, badge_x + 220, badge_y + 110),
                           radius=16,
                           fill=(30, 30, 70, 200),
                           outline=(99, 102, 241), width=2)
        # Number
        count_str = str(item_count)
        count_bbox = draw.textbbox((0, 0), count_str, font=font_badge)
        count_w = count_bbox[2] - count_bbox[0]
        draw.text((badge_x + (220 - count_w) / 2, badge_y + 10), count_str,
                  fill=accent, font=font_badge)
        # Label
        label = "精选条目" if lang == "zh" else "CURATED"
        label_bbox = draw.textbbox((0, 0), label, font=font_badge_label)
        label_w = label_bbox[2] - label_bbox[0]
        draw.text((badge_x + (220 - label_w) / 2, badge_y + 75), label,
                  fill=(148, 163, 184), font=font_badge_label)

        # ── 13. Top headlines ──
        if items:
            top_titles = _extract_top_titles(items, lang, count=3)
        else:
            top_titles = []

        if top_titles:
            hl_start_y = h * 0.50
            hl_x = w * 0.08
            section_label = "本周亮点" if lang == "zh" else "THIS WEEK'S HIGHLIGHTS"
            draw.text((hl_x, hl_start_y - 45), section_label,
                      fill=(99, 102, 241), font=font_badge_label)

            for i, title in enumerate(top_titles):
                y = hl_start_y + i * 55

                # Number circle
                num_str = str(i + 1)
                circle_cx = hl_x + 16
                circle_cy = y + 16
                draw.ellipse(
                    [circle_cx - 16, circle_cy - 16, circle_cx + 16, circle_cy + 16],
                    fill=accent,
                )
                num_bbox = draw.textbbox((0, 0), num_str, font=font_headline_num)
                num_w = num_bbox[2] - num_bbox[0]
                num_h = num_bbox[3] - num_bbox[1]
                draw.text((circle_cx - num_w / 2, circle_cy - num_h / 2 - 2),
                          num_str, fill=(255, 255, 255), font=font_headline_num)

                # Title text
                draw.text((hl_x + 45, y), title,
                          fill=(226, 232, 240), font=font_headline)

        # ── 14. Bottom bar ──
        bar_y = h - 60
        draw.rectangle([(0, bar_y), (w, h)], fill=(10, 10, 30))
        draw.rectangle([(0, bar_y), (w, bar_y + 2)], fill=(99, 102, 241, 80))

        # Watermark
        wm_text = "Generated by AIteller  |  github.com/anthropics"
        draw.text((w * 0.08, bar_y + 18), wm_text,
                  fill=(100, 116, 139), font=font_watermark)

        # ── 15. Corner decorations ──
        # Top-left bracket
        bracket_color = (99, 102, 241, 100)
        draw.line([(30, 30), (30, 80)], fill=bracket_color, width=3)
        draw.line([(30, 30), (80, 30)], fill=bracket_color, width=3)
        # Bottom-right bracket
        draw.line([(w - 30, h - 80), (w - 30, h - 30)], fill=bracket_color, width=3)
        draw.line([(w - 80, h - 30), (w - 30, h - 30)], fill=bracket_color, width=3)

        # ── Save ──
        output_dir.mkdir(parents=True, exist_ok=True)
        cover_path = output_dir / f"cover_{week_start}_{week_end}_{lang}.png"
        img.save(str(cover_path), "PNG")
        logger.info("Cover image: %s", cover_path)
        return cover_path

    def compose(self, cover_path: Path, audio_path: Path, output_path: Path) -> Path:
        """Combine static cover image + audio into MP4 using ffmpeg."""
        ffmpeg = _check_ffmpeg()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            ffmpeg,
            "-loop", "1",
            "-i", str(cover_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-y",
            str(output_path),
        ]

        logger.info("ffmpeg: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            logger.error("ffmpeg stderr: %s", result.stderr[-500:] if result.stderr else "")
            raise RuntimeError(f"ffmpeg failed with code {result.returncode}")

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("Video: %s (%.1f MB)", output_path.name, size_mb)
        return output_path
