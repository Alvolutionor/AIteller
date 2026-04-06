# src/report/generator.py
"""Generate well-formatted PDF reports with i18n support."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

from src.report.i18n import get_strings

logger = logging.getLogger(__name__)


def _get_tier(score: float) -> int:
    if score >= 6.0:
        return 5
    elif score >= 5.0:
        return 4
    elif score >= 4.0:
        return 3
    return 2


class ReportPDF(FPDF):
    """PDF with CJK font support and AIteller styling."""

    def __init__(self, lang: str = "en"):
        super().__init__()
        self.lang = lang
        self._s = get_strings(lang)
        self._zh = self._setup_fonts()
        self.set_auto_page_break(auto=True, margin=20)

    def _setup_fonts(self) -> str:
        """Load CJK fonts. Returns font family name."""
        candidates = [
            ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
            ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
            ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
        for regular, bold in candidates:
            if os.path.exists(regular):
                try:
                    self.add_font("zh", "", regular)
                    self.add_font("zh", "B", bold)
                    return "zh"
                except Exception as e:
                    logger.warning("Failed to load font %s: %s", regular, e)
                    continue
        raise RuntimeError(self._s["font_error"])

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font(self._zh, "B", 9)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, self._s["daily_header"], align="L")
        self.ln(10)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font(self._zh, "", 8)
        self.set_text_color(160, 160, 160)
        self.cell(0, 10, self._s["page_footer"].format(page=self.page_no()), align="C")


class ReportGenerator:
    """Generate PDF digest reports."""

    def __init__(self, config: dict, lang: str = "en"):
        self.config = config
        self.lang = lang
        self._s = get_strings(lang)
        self.output_dir = Path(
            config.get("output", {}).get("daily_dir", "./output/daily")
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, items: list[dict], date_str: str = None) -> Path:
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        s = self._s
        pdf = ReportPDF(lang=self.lang)
        zh = pdf._zh

        # --- Title Page ---
        pdf.add_page()
        pdf.ln(50)
        pdf.set_font(zh, "B", 32)
        pdf.set_text_color(33, 33, 33)
        pdf.cell(0, 16, s["daily_title"], align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font(zh, "", 16)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 10, date_str, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(12)

        total = len(items)
        sources = set(item.get("source", "") for item in items)
        highlights = [i for i in items if i.get("score_total", 0) >= 3.5]
        pdf.set_font(zh, "", 12)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(
            0, 8,
            s["daily_stats"].format(total=total, sources=len(sources), highlights=len(highlights)),
            align="C", new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(20)

        source_counts = {}
        for item in items:
            src = item.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        pdf.set_font(zh, "", 10)
        pdf.set_text_color(120, 120, 120)
        unit = s["daily_source_unit"]
        src_names = s["sources"]
        parts = [
            f"{src_names.get(src, src)} {cnt}{unit}"
            for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])
        ]
        pdf.cell(0, 8, s["daily_source_prefix"] + "  |  ".join(parts), align="C",
                 new_x="LMARGIN", new_y="NEXT")

        # --- Content Pages ---
        sorted_items = sorted(items, key=lambda x: x.get("score_total", 0), reverse=True)
        tier_labels = s["tier_daily"]

        tiers = {}
        for item in sorted_items:
            tier = _get_tier(item.get("score_total", 0))
            tiers.setdefault(tier, []).append(item)

        for tier_level in (5, 4, 3, 2):
            tier_items = tiers.get(tier_level, [])
            if not tier_items:
                continue
            label, color = tier_labels[tier_level]
            pdf.add_page()
            header_text = f"{label} ({len(tier_items)})"
            self._render_section_header(pdf, zh, header_text, color)
            for item in tier_items:
                self._render_item(pdf, zh, item, s)

        # --- Footer Page ---
        pdf.add_page()
        pdf.ln(40)
        pdf.set_font(zh, "", 11)
        pdf.set_text_color(130, 130, 130)
        for line in [s["daily_footer_1"], s["daily_footer_2"], s["daily_footer_3"]]:
            pdf.cell(0, 8, line, align="C", new_x="LMARGIN", new_y="NEXT")

        filename = s["daily_filename"].format(date=date_str)
        filepath = self.output_dir / filename
        pdf.output(str(filepath))
        logger.info("Report generated: %s (%d items)", filepath, total)
        return filepath

    def _render_section_header(self, pdf, zh, title, color):
        r, g, b = color
        pdf.set_fill_color(r, g, b)
        pdf.rect(10, pdf.get_y(), 4, 12, style="F")
        pdf.set_x(18)
        pdf.set_font(zh, "B", 16)
        pdf.set_text_color(r, g, b)
        pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_draw_color(220, 220, 220)
        pdf.line(10, pdf.get_y(), pdf.w - 10, pdf.get_y())
        pdf.ln(6)

    def _render_item(self, pdf, zh, item, s):
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()

        score = item.get("score_total", 0)
        tier = _get_tier(score)
        _, color = s["tier_daily"][tier]
        source = s["sources"].get(item.get("source", ""), item.get("source", ""))
        title = item.get("title", s["no_title"])
        url = item.get("url", "")

        # Parse summary JSON
        summary_raw = item.get("summary", "")
        summary = ""
        if isinstance(summary_raw, str):
            try:
                obj = json.loads(summary_raw)
                if isinstance(obj, dict):
                    if self.lang == "en":
                        title = obj.get("title_en") or title
                        summary = item.get("content", "") or obj.get("summary", "")
                    else:
                        title = obj.get("title_zh", obj.get("title", title)) or title
                        summary = obj.get("summary", "")
                else:
                    summary = summary_raw
            except (json.JSONDecodeError, TypeError):
                summary = summary_raw

        # Score badge
        r, g, b = color
        pdf.set_text_color(r, g, b)
        pdf.set_font(zh, "B", 10)
        pdf.cell(18, 7, f"[{score:.1f}]")

        # Source tag
        pdf.set_font(zh, "", 9)
        pdf.set_text_color(100, 100, 100)
        src_w = pdf.get_string_width(f"[{source}]") + 2
        pdf.cell(src_w, 7, f"[{source}]")

        # Title
        pdf.set_font(zh, "B", 11)
        remaining_w = pdf.w - pdf.get_x() - 10
        if url:
            pdf.set_text_color(20, 60, 120)
            pdf.multi_cell(remaining_w, 7, title, new_x="LMARGIN", new_y="NEXT", link=url)
        else:
            pdf.set_text_color(33, 33, 33)
            pdf.multi_cell(remaining_w, 7, title, new_x="LMARGIN", new_y="NEXT")

        # Metadata
        author = item.get("author", "")
        pub_date = item.get("published_at", "")
        meta_parts = []
        if author:
            meta_parts.append(f"{s['author']} {author}")
        if pub_date:
            meta_parts.append(str(pub_date)[:10])
        metadata = item.get("metadata", "")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        views = metadata.get("views") or metadata.get("play") or metadata.get("view_count")
        if views:
            meta_parts.append(f"{views} {s['views']}")
        likes = metadata.get("likes") or metadata.get("like_count")
        if likes:
            meta_parts.append(f"{likes} {s['likes']}")

        if meta_parts:
            pdf.set_font(zh, "", 8)
            pdf.set_text_color(140, 140, 140)
            pdf.set_x(18)
            pdf.cell(0, 5, "  |  ".join(meta_parts), new_x="LMARGIN", new_y="NEXT")

        if summary:
            pdf.set_font(zh, "", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(18)
            display = summary[:400] + "..." if len(summary) > 400 else summary
            pdf.multi_cell(pdf.w - 28, 6, display, new_x="LMARGIN", new_y="NEXT")

        if url:
            pdf.set_font(zh, "", 7)
            pdf.set_text_color(70, 130, 180)
            pdf.set_x(18)
            display_url = url if len(url) <= 90 else url[:87] + "..."
            pdf.cell(0, 5, display_url, new_x="LMARGIN", new_y="NEXT", link=url)

        pdf.ln(4)
        pdf.set_draw_color(235, 235, 235)
        pdf.line(18, pdf.get_y(), pdf.w - 18, pdf.get_y())
        pdf.ln(4)
