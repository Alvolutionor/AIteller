# src/report/generator.py
"""Generate well-formatted Chinese PDF reports for WeChat distribution."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

logger = logging.getLogger(__name__)

# Source name mapping to Chinese
SOURCE_ZH = {
    "hackernews": "Hacker News",
    "reddit": "Reddit",
    "github_trending": "GitHub 趋势",
    "arxiv": "arXiv 论文",
    "youtube": "YouTube",
    "bilibili": "B站",
    "twitter": "Twitter/X",
    "rss_blogs": "技术博客",
}

# Score tier labels (calibrated for 0-10 scoring range)
TIER_LABELS = {
    5: ("必读", (220, 53, 69)),     # red
    4: ("精选", (255, 152, 0)),     # orange
    3: ("推荐", (76, 175, 80)),     # green
    2: ("参考", (158, 158, 158)),   # grey
}


def _get_tier(score: float) -> int:
    if score >= 6.0:
        return 5
    elif score >= 5.0:
        return 4
    elif score >= 4.0:
        return 3
    return 2


class ReportPDF(FPDF):
    """PDF with Chinese font support and AIteller styling."""

    def __init__(self):
        super().__init__()
        self._zh = self._setup_fonts()
        self.set_auto_page_break(auto=True, margin=20)

    def _setup_fonts(self) -> str:
        """Load Chinese fonts. Returns font family name."""
        # Preference order: SimHei (simpler .ttf), then Microsoft YaHei (.ttc)
        candidates = [
            ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
            ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
            # macOS
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
            # Linux
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
        raise RuntimeError(
            "未找到中文字体。请确保系统安装了 SimHei 或 Microsoft YaHei 字体。"
        )

    def header(self):
        if self.page_no() == 1:
            return  # Title page has custom header
        self.set_font(self._zh, "B", 9)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, "AI 实践日报 — AIteller", align="L")
        self.ln(10)
        # Separator line
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font(self._zh, "", 8)
        self.set_text_color(160, 160, 160)
        self.cell(0, 10, f"第 {self.page_no()} 页  |  由 AIteller 自动生成", align="C")


class ReportGenerator:
    """Generate PDF digest reports."""

    def __init__(self, config: dict):
        self.config = config
        self.output_dir = Path(
            config.get("output", {}).get("daily_dir", "./output/daily")
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, items: list[dict], date_str: str = None) -> Path:
        """Generate PDF report from processed items. Returns file path."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        pdf = ReportPDF()
        zh = pdf._zh

        # --- Title Page ---
        pdf.add_page()
        pdf.ln(50)
        pdf.set_font(zh, "B", 32)
        pdf.set_text_color(33, 33, 33)
        pdf.cell(0, 16, "AI 实践日报", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font(zh, "", 16)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 10, date_str, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(12)

        # Stats summary
        total = len(items)
        sources = set(item.get("source", "") for item in items)
        highlights = [i for i in items if i.get("score_total", 0) >= 3.5]
        pdf.set_font(zh, "", 12)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(
            0, 8,
            f"共收录 {total} 篇内容  |  {len(sources)} 个来源  |  {len(highlights)} 篇精选",
            align="C", new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(20)

        # Source breakdown
        source_counts = {}
        for item in items:
            src = item.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        pdf.set_font(zh, "", 10)
        pdf.set_text_color(120, 120, 120)
        parts = [
            f"{SOURCE_ZH.get(src, src)} {cnt}篇"
            for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])
        ]
        pdf.cell(0, 8, "来源：" + "  |  ".join(parts), align="C",
                 new_x="LMARGIN", new_y="NEXT")

        # --- Content Pages ---
        # Sort by score descending
        sorted_items = sorted(items, key=lambda x: x.get("score_total", 0), reverse=True)

        # Group by tier
        tiers = {}
        for item in sorted_items:
            tier = _get_tier(item.get("score_total", 0))
            tiers.setdefault(tier, []).append(item)

        for tier_level in (5, 4, 3, 2):
            tier_items = tiers.get(tier_level, [])
            if not tier_items:
                continue

            label, color = TIER_LABELS[tier_level]
            pdf.add_page()
            self._render_section_header(pdf, zh, f"{label}（{len(tier_items)} 篇）", color)

            for item in tier_items:
                self._render_item(pdf, zh, item)

        # --- Footer Page ---
        pdf.add_page()
        pdf.ln(40)
        pdf.set_font(zh, "", 11)
        pdf.set_text_color(130, 130, 130)
        pdf.cell(0, 8, "以上内容由 AIteller 自动收集、评分和摘要生成。", align="C",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, "信息来源包括 Hacker News、Reddit、GitHub、arXiv、YouTube、B站 等。",
                 align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, "如有疑问请以原文链接为准。", align="C",
                 new_x="LMARGIN", new_y="NEXT")

        # Save
        filename = f"AI实践日报_{date_str}.pdf"
        filepath = self.output_dir / filename
        pdf.output(str(filepath))
        logger.info("Report generated: %s (%d items)", filepath, total)
        return filepath

    def _render_section_header(self, pdf: ReportPDF, zh: str,
                               title: str, color: tuple):
        """Render a colored section header."""
        r, g, b = color
        # Colored bar
        pdf.set_fill_color(r, g, b)
        pdf.rect(10, pdf.get_y(), 4, 12, style="F")
        pdf.set_x(18)
        pdf.set_font(zh, "B", 16)
        pdf.set_text_color(r, g, b)
        pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        # Thin separator
        pdf.set_draw_color(220, 220, 220)
        pdf.line(10, pdf.get_y(), pdf.w - 10, pdf.get_y())
        pdf.ln(6)

    def _render_item(self, pdf: ReportPDF, zh: str, item: dict):
        """Render a single item entry with clickable links."""
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()

        score = item.get("score_total", 0)
        tier = _get_tier(score)
        _, color = TIER_LABELS[tier]
        source = SOURCE_ZH.get(item.get("source", ""), item.get("source", ""))
        title = item.get("title", "无标题")
        url = item.get("url", "")

        # Parse summary JSON for Chinese title
        summary_raw = item.get("summary", "")
        summary = ""
        if isinstance(summary_raw, str):
            try:
                s = json.loads(summary_raw)
                if isinstance(s, dict):
                    title = s.get("title_zh", title) or title
                    summary = s.get("summary", "")
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

        # Title — clickable
        pdf.set_font(zh, "B", 11)
        remaining_w = pdf.w - pdf.get_x() - 10
        if url:
            pdf.set_text_color(20, 60, 120)
            pdf.multi_cell(remaining_w, 7, title, new_x="LMARGIN", new_y="NEXT",
                           link=url)
        else:
            pdf.set_text_color(33, 33, 33)
            pdf.multi_cell(remaining_w, 7, title, new_x="LMARGIN", new_y="NEXT")

        # Metadata line
        author = item.get("author", "")
        pub_date = item.get("published_at", "")
        meta_parts = []
        if author:
            meta_parts.append(f"作者: {author}")
        if pub_date:
            meta_parts.append(str(pub_date)[:10])
        # Parse metadata for engagement
        metadata = item.get("metadata", "")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        views = metadata.get("views") or metadata.get("play") or metadata.get("view_count")
        if views:
            meta_parts.append(f"浏览 {views}")
        likes = metadata.get("likes") or metadata.get("like_count")
        if likes:
            meta_parts.append(f"点赞 {likes}")

        if meta_parts:
            pdf.set_font(zh, "", 8)
            pdf.set_text_color(140, 140, 140)
            pdf.set_x(18)
            pdf.cell(0, 5, "  |  ".join(meta_parts), new_x="LMARGIN", new_y="NEXT")

        # Summary
        if summary:
            pdf.set_font(zh, "", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(18)
            display = summary[:400] + "..." if len(summary) > 400 else summary
            pdf.multi_cell(pdf.w - 28, 6, display, new_x="LMARGIN", new_y="NEXT")

        # Clickable URL
        if url:
            pdf.set_font(zh, "", 7)
            pdf.set_text_color(70, 130, 180)
            pdf.set_x(18)
            display_url = url if len(url) <= 90 else url[:87] + "..."
            pdf.cell(0, 5, display_url, new_x="LMARGIN", new_y="NEXT", link=url)

        # Separator
        pdf.ln(4)
        pdf.set_draw_color(235, 235, 235)
        pdf.line(18, pdf.get_y(), pdf.w - 18, pdf.get_y())
        pdf.ln(4)
