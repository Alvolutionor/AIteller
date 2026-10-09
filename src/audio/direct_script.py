"""Direct-from-DB audio script builder.

Skips the LLM entirely — pulls the same `title_zh + summary` pairs the weekly
PDF uses and stitches them into flowing spoken prose. Avoids token limits,
hallucination, and "30% of items quietly dropped" symptoms.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from src.prompts.filter import TIER_MAP

logger = logging.getLogger(__name__)

SOURCE_SPEECH = {
    "hackernews": "Hacker News",
    "reddit": "Reddit",
    "github_trending": "GitHub Trending",
    "arxiv": "arXiv",
    "youtube": "YouTube",
    "bilibili": "B 站",
    "twitter": "Twitter",
    "rss_blogs": "技术博客",
    "hf_papers": "Hugging Face Papers",
    "media": "媒体报道",
    "linkedin": "LinkedIn",
}

TIER_INTRO_ZH = {
    1: "先看这周实践经验类的条目。",
    2: "接下来是技术深度类内容。",
    3: "再看这周的宏观视野与趋势。",
    0: "最后是花边与创意速览。",
}

TIER_INTRO_EN = {
    1: "Now the practice-experience items.",
    2: "Next, technical deep dives.",
    3: "Broader trends and signals.",
    0: "And finally, the novelty and creative picks.",
}


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract(item: dict, lang: str) -> tuple[str, str, str]:
    """Return (source_speech, title, summary) for one item."""
    src_key = (item.get("source") or "").lower()
    source = SOURCE_SPEECH.get(src_key, src_key or "未知来源")

    summary_data = _json(item.get("summary")) or {}
    if lang == "en":
        title = summary_data.get("title_en") or item.get("title") or ""
        summary = item.get("content") or summary_data.get("summary") or ""
    else:
        title = (summary_data.get("title_zh")
                 or summary_data.get("title")
                 or item.get("title") or "")
        summary = summary_data.get("summary") or ""

    return source.strip(), title.strip(), summary.strip()


def _tier_of(item: dict) -> int:
    tags = _json(item.get("tags"))
    category = tags[0] if isinstance(tags, list) and tags else "personal_exp"
    return TIER_MAP.get(category, 3)


def _group_by_tier(items: Iterable[dict]) -> dict[int, list[dict]]:
    groups: dict[int, list[dict]] = {1: [], 2: [], 3: [], 0: []}
    for it in items:
        groups.setdefault(_tier_of(it), []).append(it)
    for tier in groups:
        groups[tier].sort(key=lambda x: float(x.get("score_total") or 0), reverse=True)
    return groups


def _truncate_summary(summary: str, max_chars: int, sentence_enders: str) -> str:
    """Keep first few sentences up to max_chars; trim at nearest sentence end."""
    if not summary or len(summary) <= max_chars:
        return summary
    cut = summary[:max_chars]
    last_end = -1
    for ch in sentence_enders:
        last_end = max(last_end, cut.rfind(ch))
    if last_end >= max_chars // 2:
        return cut[: last_end + 1]
    return cut.rstrip("，,、；; ") + "。"


def _speak_item_zh(source: str, title: str, summary: str, max_summary_chars: int) -> str:
    if not title:
        return ""
    parts = [f"来自 {source} 的内容，{title}。"]
    if summary:
        clean_summary = " ".join(summary.split())
        clean_summary = _truncate_summary(clean_summary, max_summary_chars, "。！？.!?")
        parts.append(clean_summary if clean_summary.endswith(("。", "！", "？")) else clean_summary + "。")
    return " ".join(parts)


def _speak_item_en(source: str, title: str, summary: str, max_summary_chars: int) -> str:
    if not title:
        return ""
    parts = [f"From {source}: {title}."]
    if summary:
        clean_summary = " ".join(summary.split())
        clean_summary = _truncate_summary(clean_summary, max_summary_chars, ".!?")
        parts.append(clean_summary if clean_summary.endswith((".", "!", "?")) else clean_summary + ".")
    return " ".join(parts)


def build_direct_script(items: list[dict], week_start: str, week_end: str,
                        lang: str = "zh",
                        max_summary_chars: int = 120,
                        skip_novelty: bool = True) -> str:
    """Return a plain-text script built directly from items. No LLM call."""
    if not items:
        return ""

    groups = _group_by_tier(items)
    covered_tiers = [1, 2, 3] if skip_novelty else [1, 2, 3, 0]
    total = sum(len(groups.get(t, [])) for t in covered_tiers)

    lines: list[str] = []

    if lang == "zh":
        lines.append(
            f"本期 AI 工程周报，覆盖 {week_start} 到 {week_end}，共 {total} 条精选内容。"
        )
    else:
        lines.append(
            f"AI engineering weekly, {week_start} to {week_end}, {total} curated items."
        )
    lines.append("")

    tier_intro = TIER_INTRO_ZH if lang == "zh" else TIER_INTRO_EN
    tier_order = [1, 2, 3] if skip_novelty else [1, 2, 3, 0]
    speak = _speak_item_zh if lang == "zh" else _speak_item_en

    for tier in tier_order:
        bucket = groups.get(tier, [])
        if not bucket:
            continue
        lines.append(tier_intro[tier])
        lines.append("")
        for item in bucket:
            source, title, summary = _extract(item, lang)
            sentence = speak(source, title, summary, max_summary_chars)
            if sentence:
                lines.append(sentence)
                lines.append("")

    if lang == "zh":
        lines.append("以上是本期全部内容。详细条目与源链接见 PDF。")
    else:
        lines.append("That's all for this week. Full item list and source links are in the PDF.")

    script = "\n".join(lines).rstrip() + "\n"
    logger.info("Direct script built: %d items, %d chars (%s)", total, len(script), lang)
    return script
