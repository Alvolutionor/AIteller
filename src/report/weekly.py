"""Weekly PDF report generator with dense two-column layout."""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

from src.prompts.filter import CATEGORIES, CATEGORY_IDS, TIER_MAP
from src.report.i18n import get_strings, get_category_name

logger = logging.getLogger(__name__)

SOURCE_REPORT_HEADER = {
    "hackernews": "HackerNews",
    "reddit": "Reddit",
    "github_trending": "GitHub",
    "arxiv": "arXiv",
    "youtube": "YouTube",
    "bilibili": "Bilibili",
    "twitter": "Twitter",
    "rss_blogs": "Blogs",
    "hf_papers": "HF Papers",
}

SOURCE_ORDER = [
    "hackernews",
    "reddit",
    "github_trending",
    "arxiv",
    "rss_blogs",
    "twitter",
    "bilibili",
    "youtube",
    "hf_papers",
]

# Module-level lang — set by WeeklyReportGenerator before generate()
_lang = "en"

PROBLEMATIC_REPLACEMENTS = {
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\ufeff": "",
    "\xa0": " ",
    "™": "TM",
    "®": "(R)",
    "©": "(C)",
    "〜": "~",
    "–": "-",
    "—": "-",
    "•": "-",
    "·": "·",
}


def _score(item: dict) -> float:
    try:
        return float(item.get("score_total") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _category_name(category_id: str) -> str:
    return get_category_name(category_id, _lang)


def _parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _clean_pdf_text(text: str | None) -> str:
    if not text:
        return ""

    cleaned = str(text)
    for src, dst in PROBLEMATIC_REPLACEMENTS.items():
        cleaned = cleaned.replace(src, dst)

    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    allowed = []
    for ch in cleaned:
        code = ord(ch)
        if ch == "\n" or ch == "\t":
            allowed.append(ch)
            continue
        if 32 <= code <= 126:
            allowed.append(ch)
            continue
        if 0x4E00 <= code <= 0x9FFF:
            allowed.append(ch)
            continue
        if 0x3000 <= code <= 0x303F:
            allowed.append(ch)
            continue
        if 0xFF00 <= code <= 0xFFEF:
            allowed.append(ch)
            continue

    collapsed = "".join(allowed)
    lines = [" ".join(line.split()) for line in collapsed.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _get_category(item: dict) -> str:
    tags = _parse_json(item.get("tags"))
    if isinstance(tags, list) and tags:
        category = tags[0]
        if category in CATEGORY_IDS:
            return category
    category = item.get("category")
    if category in CATEGORY_IDS:
        return category
    return "personal_exp"


def _extract_display_content(item: dict) -> tuple[str, str]:
    s = get_strings(_lang)
    title = item.get("title", s["no_title"])
    summary = item.get("summary", "")

    summary_data = _parse_json(summary)
    if isinstance(summary_data, dict):
        title_key = "title_en" if _lang == "en" else "title_zh"
        title = summary_data.get(title_key) or summary_data.get("title_zh") or summary_data.get("title") or title
        summary = summary_data.get("summary") or ""
    elif not summary:
        summary = item.get("content", "") or ""

    title = _clean_pdf_text(title)
    summary = _clean_pdf_text(summary)
    return title, summary


def _present_sources(items: list[dict]) -> list[str]:
    counts = defaultdict(int)
    for item in items:
        counts[item.get("source", "unknown")] += 1

    ordered = [source for source in SOURCE_ORDER if counts.get(source)]
    extras = sorted(
        (source for source in counts if source not in SOURCE_ORDER),
        key=lambda source: (-counts[source], source),
    )
    return ordered + extras


def _category_distribution_rows(items: list[dict], sources: list[str], include_novelty: bool = False) -> list[dict]:
    counts_by_category = {
        category_id: {source: 0 for source in sources}
        for category_id in CATEGORY_IDS
    }

    for item in items:
        category_id = _get_category(item)
        source = item.get("source", "unknown")
        if category_id not in counts_by_category or source not in counts_by_category[category_id]:
            continue
        counts_by_category[category_id][source] += 1

    rows = []
    ordered_categories = [category_id for category_id in CATEGORY_IDS if include_novelty or category_id != "novelty"]
    for category_id in ordered_categories:
        counts = counts_by_category[category_id]
        total = sum(counts.values())
        if total == 0:
            continue
        rows.append(
            {
                "category_id": category_id,
                "category_name": _category_name(category_id),
                "counts": counts,
                "total": total,
                "tier": TIER_MAP.get(category_id, 3),
            }
        )

    rows.sort(key=lambda row: (row["tier"], -row["total"], row["category_name"]))
    return rows


def _group_items_by_tier_and_category(items: list[dict]) -> dict[int, dict[str, list[dict]]]:
    grouped: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for item in items:
        category_id = _get_category(item)
        tier = TIER_MAP.get(category_id, 3)
        grouped[tier][category_id].append(item)

    result: dict[int, dict[str, list[dict]]] = {}
    for tier, categories in grouped.items():
        ordered_categories = sorted(
            categories.items(),
            key=lambda entry: (-len(entry[1]), -max(_score(item) for item in entry[1]), _category_name(entry[0])),
        )
        result[tier] = {}
        for category_id, category_items in ordered_categories:
            result[tier][category_id] = sorted(category_items, key=_score, reverse=True)
    return result


def _source_stats(items: list[dict]) -> list[dict]:
    bucket: dict[str, list[float]] = defaultdict(list)
    for item in items:
        bucket[item.get("source", "unknown")].append(_score(item))

    stats = []
    for source in _present_sources(items):
        scores = bucket.get(source, [])
        avg_score = sum(scores) / len(scores) if scores else 0.0
        stats.append(
            {
                "source": source,
                "count": len(scores),
                "avg_score": avg_score,
            }
        )
    return stats


def _fmt_compact_number(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""

    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return str(number)


def _render_score_badge(pdf: FPDF, font_name: str, x: float, y: float, score: float):
    if score >= 6.0:
        fill = (183, 28, 28)
    elif score >= 5.0:
        fill = (194, 65, 12)
    elif score >= 4.0:
        fill = (21, 128, 61)
    else:
        fill = (100, 116, 139)
    pdf.set_xy(x, y)
    pdf.set_fill_color(*fill)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font(font_name, "B", 8)
    pdf.cell(12, 5, f"{score:.1f}", border=0, align="C", fill=True)


def _build_meta_text(item: dict) -> str:
    metadata = _parse_json(item.get("metadata")) or {}
    s = get_strings(_lang)
    parts = [s["sources"].get(item.get("source", ""), item.get("source", ""))]

    author = _clean_pdf_text(item.get("author", ""))
    if author:
        parts.append(author)

    views = metadata.get("views") or metadata.get("play") or metadata.get("view_count")
    likes = metadata.get("likes") or metadata.get("like_count")
    comments = metadata.get("comments") or metadata.get("comment_count")

    if views:
        parts.append(f"▶ {_fmt_compact_number(views)}")
    if likes:
        parts.append(f"♥ {_fmt_compact_number(likes)}")
    if comments:
        parts.append(f"💬 {_fmt_compact_number(comments)}")

    return _clean_pdf_text(" · ".join(part for part in parts if part))


class WeeklyReportPDF(FPDF):
    def __init__(self, lang: str = "en"):
        super().__init__(orientation="L", format="A4")
        self._s = get_strings(lang)
        self._zh = self._setup_fonts()
        self.set_margins(10, 10, 10)
        self.set_auto_page_break(auto=False)

    def _setup_fonts(self) -> str:
        candidates = [
            ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
            ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
            ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
        for regular, bold in candidates:
            if not os.path.exists(regular):
                continue
            try:
                self.add_font("zh", "", regular)
                self.add_font("zh", "B", bold)
                return "zh"
            except Exception as exc:
                logger.warning("Failed to load font %s: %s", regular, exc)
        raise RuntimeError(self._s["font_error"])

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font(self._zh, "B", 9)
        self.set_text_color(71, 85, 105)
        self.cell(0, 6, self._s["weekly_header"], new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(226, 232, 240)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-10)
        self.set_font(self._zh, "", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 5, self._s["page_footer"].format(page=self.page_no()), align="C")


class WeeklyReportGenerator:
    """Generate weekly PDF report with experiment-style statistics and dense layout."""

    def __init__(self, config: dict, lang: str = "en"):
        self.config = config
        self.lang = lang
        self._s = get_strings(lang)
        self.output_dir = Path(config.get("output", {}).get("weekly_dir", "./output/weekly"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, items: list[dict], week_start: str, week_end: str) -> Path:
        global _lang
        _lang = self.lang
        s = self._s

        pdf = WeeklyReportPDF(lang=self.lang)
        pdf.add_page()
        self._render_cover_page(pdf, items, week_start, week_end)

        grouped = _group_items_by_tier_and_category(items)
        for tier in (1, 2, 3, 0):
            categories = grouped.get(tier)
            if not categories:
                continue
            self._render_tier_section(pdf, tier, categories)

        filename = s["weekly_filename"].format(start=week_start, end=week_end)
        output_path = self.output_dir / filename
        pdf.output(str(output_path))
        logger.info("Weekly report generated: %s (%d items)", output_path, len(items))
        return output_path

    def _render_cover_page(self, pdf: WeeklyReportPDF, items: list[dict], week_start: str, week_end: str):
        zh = pdf._zh
        s = self._s
        pdf.set_fill_color(15, 23, 42)
        pdf.rect(10, 10, pdf.w - 20, 28, style="F")
        pdf.set_xy(16, 17)
        pdf.set_font(zh, "B", 24)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 10, s["weekly_title"])
        pdf.ln(11)
        pdf.set_x(16)
        pdf.set_font(zh, "", 11)
        pdf.set_text_color(203, 213, 225)
        pdf.cell(0, 6, s["weekly_subtitle"].format(start=week_start, end=week_end, count=len(items)))

        self._render_top_category_bar(pdf, items)
        self._render_source_overview(pdf, items)
        self._render_category_distribution(pdf, items)

    def _render_top_category_bar(self, pdf: WeeklyReportPDF, items: list[dict]):
        zh = pdf._zh
        counts = defaultdict(int)
        for item in items:
            counts[_get_category(item)] += 1

        top_categories = sorted(counts.items(), key=lambda entry: (-entry[1], _category_name(entry[0])))[:4]
        other_count = len(items) - sum(count for _, count in top_categories)

        cards = list(top_categories)
        if other_count > 0:
            cards.append(("other", other_count))

        card_y = 42
        usable_w = pdf.w - 20
        gap = 4
        card_w = (usable_w - gap * (len(cards) - 1)) / len(cards)

        for index, (category_id, count) in enumerate(cards):
            x = 10 + index * (card_w + gap)
            pdf.set_fill_color(248, 250, 252)
            pdf.set_draw_color(226, 232, 240)
            pdf.rect(x, card_y, card_w, 18, style="FD")
            pdf.set_xy(x, card_y + 2)
            pdf.set_font(zh, "B", 15)
            pdf.set_text_color(30, 64, 175 if category_id == "other" else 30)
            pdf.cell(card_w, 6, str(count), align="C")
            pdf.ln(6)
            pdf.set_x(x)
            pdf.set_font(zh, "", 8)
            pdf.set_text_color(71, 85, 105)
            label = self._s["weekly_other"] if category_id == "other" else _category_name(category_id)
            pdf.cell(card_w, 5, _clean_pdf_text(label), align="C")

    def _render_source_overview(self, pdf: WeeklyReportPDF, items: list[dict]):
        zh = pdf._zh
        s = self._s
        src_names = s["sources"]
        stats = _source_stats(items)
        y = 66

        pdf.set_xy(10, y)
        pdf.set_font(zh, "B", 12)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(0, 7, s["weekly_source_overview"], new_x="LMARGIN", new_y="NEXT")

        widths = [48, 28, 26]
        table_x = 10
        table_y = pdf.get_y()

        headers = s["weekly_source_headers"]
        pdf.set_fill_color(241, 245, 249)
        pdf.set_text_color(15, 23, 42)
        pdf.set_font(zh, "B", 9)
        for width, header in zip(widths, headers):
            pdf.cell(width, 7, header, border=1, fill=True, align="C")
        pdf.ln(7)

        pdf.set_font(zh, "", 8)
        for row in stats:
            pdf.set_x(table_x)
            pdf.set_text_color(51, 65, 85)
            pdf.cell(widths[0], 6, _clean_pdf_text(src_names.get(row["source"], row["source"])), border=1)
            pdf.cell(widths[1], 6, str(row["count"]), border=1, align="C")
            pdf.cell(widths[2], 6, f"{row['avg_score']:.2f}", border=1, align="C")
            pdf.ln(6)
        left_bottom_y = pdf.get_y()

        pdf.set_xy(118, table_y - 7)
        pdf.set_font(zh, "B", 12)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(0, 7, s["weekly_notes_title"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(118)
        pdf.set_font(zh, "", 8.5)
        pdf.set_text_color(71, 85, 105)
        pdf.multi_cell(168, 5, _clean_pdf_text(s["weekly_notes_body"]))
        right_bottom_y = pdf.get_y()

        pdf.set_y(max(left_bottom_y, right_bottom_y) + 4)

    def _render_category_distribution(self, pdf: WeeklyReportPDF, items: list[dict]):
        zh = pdf._zh
        s = self._s
        pdf.set_font(zh, "B", 12)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(0, 7, s["weekly_category_dist"], new_x="LMARGIN", new_y="NEXT")

        preferred_sources = [
            source for source in [
                "hackernews",
                "reddit",
                "github_trending",
                "rss_blogs",
                "twitter",
                "bilibili",
                "youtube",
            ]
            if any(item.get("source") == source for item in items)
        ]
        extra_sources = [
            source for source in _present_sources(items)
            if source not in preferred_sources and source != "novelty"
        ]
        sources = preferred_sources + extra_sources
        rows = _category_distribution_rows(items, sources, include_novelty=False)
        novelty_count = sum(1 for item in items if _get_category(item) == "novelty")

        total_w = pdf.w - pdf.l_margin - pdf.r_margin
        category_w = 47
        total_col_w = 16
        source_w = (total_w - category_w - total_col_w) / max(1, len(sources))

        pdf.set_font(zh, "B", 7.6)
        pdf.set_fill_color(241, 245, 249)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(category_w, 7, s["weekly_category_header"], border=1, fill=True, align="C")
        for source in sources:
            pdf.cell(source_w, 7, SOURCE_REPORT_HEADER.get(source, s["source_alias"].get(source, source[:8])), border=1, fill=True, align="C")
        pdf.cell(total_col_w, 7, s["weekly_total"], border=1, fill=True, align="C")
        pdf.ln(7)

        pdf.set_font(zh, "", 7.4)
        tier_labels = s["tier_weekly"]
        for row in rows:
            tier = row["tier"]
            pdf.set_x(pdf.l_margin)
            pdf.set_fill_color(*tier_labels[tier][2])
            pdf.set_text_color(15, 23, 42)
            pdf.cell(category_w, 6, _clean_pdf_text(row["category_name"]), border=1, fill=True)
            for source in sources:
                pdf.cell(source_w, 6, str(row["counts"][source]), border=1, align="C")
            pdf.set_font(zh, "B", 7.4)
            pdf.cell(total_col_w, 6, str(row["total"]), border=1, align="C")
            pdf.set_font(zh, "", 7.4)
            pdf.ln(6)

        if novelty_count:
            pdf.ln(2)
            pdf.set_font(zh, "", 8)
            pdf.set_text_color(100, 116, 139)
            pdf.cell(0, 5, s["weekly_novelty_note"].format(count=novelty_count))

    def _render_tier_section(self, pdf: WeeklyReportPDF, tier: int, categories: dict[str, list[dict]]):
        tier_total = sum(len(group) for group in categories.values())
        first_category = True
        for category_id, items in categories.items():
            if first_category:
                pdf.add_page()
                self._render_tier_header(pdf, tier, tier_total)
                first_category = False
            elif pdf.get_y() > pdf.h - 35:
                pdf.add_page()
                self._render_tier_header(pdf, tier, tier_total)

            self._render_category_header(pdf, category_id, len(items), tier)
            self._render_two_column_items(pdf, tier, tier_total, category_id, items)

    def _render_tier_header(self, pdf: WeeklyReportPDF, tier: int, tier_total: int):
        zh = pdf._zh
        title, color, _ = self._s["tier_weekly"][tier]
        pdf.set_fill_color(*color)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.w - pdf.l_margin - pdf.r_margin, 9, style="F")
        pdf.set_xy(pdf.l_margin + 4, pdf.get_y() + 1.5)
        pdf.set_font(zh, "B", 12)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 6, f"{title} ({tier_total})")
        pdf.ln(10)

    def _render_category_header(self, pdf: WeeklyReportPDF, category_id: str, count: int, tier: int):
        zh = pdf._zh
        _, color, fill = self._s["tier_weekly"][tier]
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.w - pdf.l_margin - pdf.r_margin, 7, style="FD")
        pdf.set_xy(pdf.l_margin + 3, pdf.get_y() + 1)
        pdf.set_font(zh, "B", 10)
        pdf.set_text_color(*color)
        pdf.cell(0, 5, f"{_category_name(category_id)} ({count})")
        pdf.ln(7.5)

    def _measure_item_height(self, pdf: WeeklyReportPDF, col_w: float, item: dict) -> float:
        zh = pdf._zh
        title, summary = _extract_display_content(item)
        meta = _build_meta_text(item)

        title_w = col_w - 14
        pdf.set_font(zh, "B", 9.2)
        title_h = pdf.multi_cell(title_w, 4.2, title or get_strings(_lang)["no_title"], dry_run=True, output="HEIGHT")
        summary_h = 0.0
        if summary:
            pdf.set_font(zh, "", 8)
            summary_h = pdf.multi_cell(col_w, 3.8, summary[:360], dry_run=True, output="HEIGHT")
        meta_h = 0.0
        if meta:
            pdf.set_font(zh, "", 7)
            meta_h = pdf.multi_cell(col_w, 3.4, meta, dry_run=True, output="HEIGHT")
        return 4 + max(18.0, title_h + summary_h + meta_h + 4)

    def _render_item_block(self, pdf: WeeklyReportPDF, x: float, y: float, col_w: float, item: dict):
        zh = pdf._zh
        score = _score(item)
        title, summary = _extract_display_content(item)
        meta = _build_meta_text(item)
        url = item.get("url", "")

        _render_score_badge(pdf, zh, x, y, score)

        pdf.set_xy(x + 14, y)
        pdf.set_font(zh, "B", 9.2)
        pdf.set_text_color(30, 41, 59)
        pdf.multi_cell(col_w - 14, 4.2, title or get_strings(_lang)["no_title"], link=url or "")

        if summary:
            pdf.set_x(x)
            pdf.set_font(zh, "", 8)
            pdf.set_text_color(71, 85, 105)
            pdf.multi_cell(col_w, 3.8, summary[:360])

        if meta:
            pdf.set_x(x)
            pdf.set_font(zh, "", 7)
            pdf.set_text_color(148, 163, 184)
            pdf.multi_cell(col_w, 3.4, meta)

    def _render_two_column_items(
        self,
        pdf: WeeklyReportPDF,
        tier: int,
        tier_total: int,
        category_id: str,
        items: list[dict],
    ):
        col_gap = 6
        col_w = (pdf.w - pdf.l_margin - pdf.r_margin - col_gap) / 2
        bottom_limit = pdf.h - 14

        for index in range(0, len(items), 2):
            left = items[index]
            right = items[index + 1] if index + 1 < len(items) else None

            row_h = self._measure_item_height(pdf, col_w, left)
            if right:
                row_h = max(row_h, self._measure_item_height(pdf, col_w, right))

            if pdf.get_y() + row_h > bottom_limit:
                pdf.add_page()
                self._render_tier_header(pdf, tier, tier_total)
                self._render_category_header(pdf, category_id, len(items), tier)

            row_y = pdf.get_y()
            self._render_item_block(pdf, pdf.l_margin, row_y, col_w, left)
            if right:
                self._render_item_block(pdf, pdf.l_margin + col_w + col_gap, row_y, col_w, right)

            pdf.set_y(row_y + row_h)
            pdf.set_draw_color(241, 245, 249)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(2.5)
