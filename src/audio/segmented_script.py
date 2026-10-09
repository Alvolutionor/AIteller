"""Segmented script builder.

Unlike direct_script which returns a flat string, this returns a list of
typed segments (intro / tier-header / item / outro). Each segment has
self-contained text (for TTS) and structured metadata (for slide rendering).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from src.prompts.filter import TIER_MAP, CATEGORY_ORDER

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

TIER_LABEL_ZH = {
    1: "如何用好 AI",
    2: "实践与工程",
    3: "构建与训练",
    0: "花边创意",
}
TIER_INTRO_ZH = {
    1: "先看本期最核心的：如何最好地使用 AI、用好工具与模型。",
    2: "接下来是相邻的实践与工程内容。",
    3: "再看构建、训练与部署类内容。",
    0: "最后是花边与创意速览。",
}
TIER_LABEL_EN = {1: "Using AI Well", 2: "Practice & Engineering",
                 3: "Building & Training", 0: "Novelty"}
TIER_INTRO_EN = {
    1: "First, the core: how to best use AI, tools, and models.",
    2: "Next, adjacent practice and engineering.",
    3: "Then building, training, and deployment.",
    0: "And finally, novelty and creative picks.",
}


@dataclass
class Segment:
    kind: Literal["intro", "tier", "item", "outro"]
    text: str                                # text to synthesize
    title: str = ""
    subtitle: str = ""
    body: str = ""
    source: str = ""
    counter: tuple[int, int] | None = None   # (current, total) for item slides
    tier: int | None = None
    meta: dict = field(default_factory=dict)


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


def _truncate(summary: str, max_chars: int, enders: str) -> str:
    if not summary or len(summary) <= max_chars:
        return summary
    cut = summary[:max_chars]
    last_end = max((cut.rfind(ch) for ch in enders), default=-1)
    if last_end >= max_chars // 2:
        return cut[: last_end + 1]
    return cut.rstrip("，,、；; ") + ("。" if "。" in enders else ".")


def build_segments(items: list[dict], week_start: str, week_end: str,
                   lang: str = "zh", max_summary_chars: int = 80,
                   skip_novelty: bool = True) -> list[Segment]:
    """Return ordered list of typed segments for synthesis + slides."""
    if not items:
        return []

    groups: dict[int, list[dict]] = {1: [], 2: [], 3: [], 0: []}
    for it in items:
        groups.setdefault(_tier_of(it), []).append(it)
    # Within a tier, order by the FIXED section order (CATEGORY_ORDER) first,
    # then by score within each section — so the slideshow matches the PDF's
    # deterministic layout (core "using-AI" sections first, build/training
    # last) instead of letting a high-scoring section jump ahead.
    _order_pos = {cid: i for i, cid in enumerate(CATEGORY_ORDER)}

    def _section_pos(item: dict) -> int:
        tags = _json(item.get("tags"))
        cat = tags[0] if isinstance(tags, list) and tags else ""
        return _order_pos.get(cat, len(CATEGORY_ORDER))

    for t in groups:
        groups[t].sort(key=lambda x: (_section_pos(x),
                                      -float(x.get("score_total") or 0)))

    tier_order = [1, 2, 3] if skip_novelty else [1, 2, 3, 0]
    total = sum(len(groups.get(t, [])) for t in tier_order)

    segments: list[Segment] = []

    # Top teasers: pull from Tier 1 highest-scored items for clickbait hook
    t1 = groups.get(1, [])
    top_teasers: list[str] = []
    for it in t1[:3]:
        _, t_title, _ = _extract(it, lang)
        if t_title:
            top_teasers.append(t_title)

    if lang == "zh":
        intro_text = (
            f"本期 AI 工程周报，覆盖 {week_start} 到 {week_end}，"
            f"共 {total} 条精选内容。本期爆点："
            + "；".join(t[:25] for t in top_teasers[:3])
            + "。"
        )
        segments.append(Segment(
            kind="intro", text=intro_text,
            title="AI 工程周报",
            subtitle=f"{week_start}  →  {week_end}",
            body=f"{total}",
            meta={"teasers": top_teasers, "item_count": total,
                  "hook": "本期爆点 · TOP 3"},
        ))
    else:
        intro_text = (
            f"AI engineering weekly, {week_start} to {week_end}, "
            f"{total} curated items. Top picks: "
            + "; ".join(t[:40] for t in top_teasers[:3])
            + "."
        )
        segments.append(Segment(
            kind="intro", text=intro_text,
            title="AI Engineering Weekly",
            subtitle=f"{week_start}  →  {week_end}",
            body=f"{total}",
            meta={"teasers": top_teasers, "item_count": total,
                  "hook": "TOP 3 HIGHLIGHTS"},
        ))

    enders_zh = "。！？.!?"
    enders_en = ".!?"
    enders = enders_zh if lang == "zh" else enders_en
    tier_label = TIER_LABEL_ZH if lang == "zh" else TIER_LABEL_EN
    tier_intro = TIER_INTRO_ZH if lang == "zh" else TIER_INTRO_EN

    counter = 0
    for tier in tier_order:
        bucket = groups.get(tier, [])
        if not bucket:
            continue
        # Tier header slide
        segments.append(Segment(
            kind="tier", text=tier_intro[tier],
            title=tier_label[tier], subtitle=f"{len(bucket)} 条" if lang == "zh" else f"{len(bucket)} items",
            tier=tier,
        ))
        for item in bucket:
            source, title, summary = _extract(item, lang)
            counter += 1
            summary_full = " ".join(summary.split())
            summary_speak = _truncate(summary_full, max_summary_chars, enders)
            if lang == "zh":
                text = f"来自 {source} 的内容，{title}。 {summary_speak or ''}".strip()
                if not text.endswith(("。", "！", "？")):
                    text += "。"
            else:
                text = f"From {source}: {title}. {summary_speak or ''}".strip()
                if not text.endswith((".", "!", "?")):
                    text += "."
            segments.append(Segment(
                kind="item", text=text, title=title, subtitle=source,
                body=summary_full,  # slide shows FULL summary — matches PDF content
                source=source,
                counter=(counter, total), tier=tier,
            ))

    if lang == "zh":
        outro_text = "以上是本期全部内容。详细条目与源链接见 PDF。"
        segments.append(Segment(
            kind="outro", text=outro_text,
            title="本期完", subtitle="详细条目与源链接见 PDF",
        ))
    else:
        outro_text = "That's all for this week. Full item list and source links are in the PDF."
        segments.append(Segment(
            kind="outro", text=outro_text,
            title="End of Episode", subtitle="See PDF for full list",
        ))

    return segments
