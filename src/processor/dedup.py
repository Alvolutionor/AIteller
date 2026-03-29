# src/processor/dedup.py
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from src.collectors.base import RawItem

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source"}


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    params = {k: v for k, v in parse_qs(parsed.query).items() if k not in TRACKING_PARAMS}
    clean_query = urlencode(params, doseq=True)
    return urlunparse((
        "https", parsed.netloc.lower(), parsed.path.rstrip("/"),
        parsed.params, clean_query, "",
    ))


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _engagement_score(item: RawItem) -> int:
    m = item.metadata
    return m.get("points", 0) + m.get("score", 0) + m.get("stars", 0) + m.get("views", 0)


@dataclass
class DedupResult:
    unique_items: list[RawItem]
    merged_groups: list[list[RawItem]]


def deduplicate(items: list[RawItem], existing_titles: list[str] = None,
                similarity_threshold: float = 0.85) -> DedupResult:
    # Phase 1: Exact URL dedup
    url_map: dict[str, RawItem] = {}
    for item in items:
        norm_url = _normalize_url(item.url)
        if norm_url in url_map:
            existing = url_map[norm_url]
            if _engagement_score(item) > _engagement_score(existing):
                url_map[norm_url] = item
        else:
            url_map[norm_url] = item

    candidates = list(url_map.values())

    # Phase 2: Fuzzy title dedup
    all_titles = list(existing_titles or [])
    groups: list[list[RawItem]] = []
    unique: list[RawItem] = []
    used = set()

    for i, item in enumerate(candidates):
        if i in used:
            continue
        # Check against DB existing titles
        skip = False
        for existing_title in all_titles:
            if _title_similarity(item.title, existing_title) >= similarity_threshold:
                skip = True
                break
        if skip:
            used.add(i)
            continue

        group = [item]
        for j in range(i + 1, len(candidates)):
            if j in used:
                continue
            if _title_similarity(item.title, candidates[j].title) >= similarity_threshold:
                group.append(candidates[j])
                used.add(j)

        best = max(group, key=_engagement_score)
        unique.append(best)
        if len(group) > 1:
            groups.append(group)
        used.add(i)

    return DedupResult(unique_items=unique, merged_groups=groups)
