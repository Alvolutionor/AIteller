# src/collectors/arxiv_collector.py
import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

ARXIV_API = (
    "http://export.arxiv.org/api/query"
    "?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=50"
)

# Practice-oriented keywords always included in the query
_PRACTICE_KEYWORDS = [
    "deployment",
    "production",
    "benchmark",
    "tool",
    "framework",
    "inference optimization",
    "fine-tuning",
]


class ArxivCollector(BaseCollector):
    """Collect arxiv papers via the public Atom API."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 3.0  # arxiv asks for ≥3 s between requests
        arxiv_cfg = config.get("sources", {}).get("arxiv", {})
        self.categories: list[str] = arxiv_cfg.get("categories", ["cs.CL", "cs.AI", "cs.LG"])

    async def collect(self, since: datetime) -> list[RawItem]:
        query = self._build_query()
        url = ARXIV_API.format(query=quote(query))

        try:
            async with aiohttp.ClientSession() as session:
                xml_text = await self._fetch_text(session, url)
        except Exception as exc:
            logger.error("ArxivCollector: API request failed: %s", exc)
            return []

        return self._parse_feed(xml_text, since)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_query(self) -> str:
        """Build an arxiv search query from configured categories + practice keywords."""
        # Category part: cat:cs.CL OR cat:cs.AI ...
        cat_parts = [f"cat:{c}" for c in self.categories]
        cat_query = " OR ".join(cat_parts)

        # Keyword part from config primary keywords + hardcoded practice keywords
        primary_kws: list[str] = self.config.get("keywords", {}).get("primary", [])
        all_kws = list(primary_kws) + _PRACTICE_KEYWORDS
        kw_parts = [f'ti:"{kw}" OR abs:"{kw}"' for kw in all_kws]
        kw_query = " OR ".join(kw_parts)

        return f"({cat_query}) AND ({kw_query})"

    def _parse_feed(self, xml_text: str, since: datetime) -> list[RawItem]:
        feed = feedparser.parse(xml_text)
        items: list[RawItem] = []

        for entry in feed.entries:
            published_at = self._parse_published(entry)
            if published_at is None or published_at <= since:
                continue

            title: str = entry.get("title", "").replace("\n", " ").strip()
            summary: str = entry.get("summary", "").replace("\n", " ").strip()
            link: str = entry.get("link", "")

            # Collect all links; identify PDF link
            pdf_url: str = ""
            for lnk in entry.get("links", []):
                if lnk.get("title") == "pdf" or lnk.get("type") == "application/pdf":
                    pdf_url = lnk.get("href", "")
                    break

            # Authors
            authors: list[str] = [
                a.get("name", "") for a in entry.get("authors", []) if a.get("name")
            ]

            # Categories
            categories: list[str] = [
                tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")
            ]

            items.append(
                RawItem(
                    source="arxiv",
                    title=title,
                    url=link,
                    author=", ".join(authors),
                    published_at=published_at,
                    content=summary,
                    metadata={
                        "categories": categories,
                        "pdf_url": pdf_url,
                        "authors": authors,
                    },
                )
            )

        return items

    @staticmethod
    def _parse_published(entry) -> datetime | None:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            ts = calendar.timegm(entry.published_parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        raw = entry.get("published", "")
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None
