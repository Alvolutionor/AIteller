# src/collectors/hf_papers.py
"""Hugging Face Daily Papers collector.

Uses the official HF API: https://huggingface.co/api/daily_papers
Returns papers curated by the HF community with upvote counts as quality signals.
No API key required for basic access.
"""
import logging
from datetime import datetime, timezone

import aiohttp

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

HF_DAILY_PAPERS_API = "https://huggingface.co/api/daily_papers"
HF_PAPER_DETAIL_API = "https://huggingface.co/api/papers"


class HFPapersCollector(BaseCollector):
    """Collect papers from Hugging Face Daily Papers."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 1.0

    async def collect(self, since: datetime) -> list[RawItem]:
        items: list[RawItem] = []
        # HF papers update daily — use at least 7 days lookback
        from datetime import timedelta
        effective_since = min(since, datetime.now(timezone.utc) - timedelta(days=7))

        try:
            async with aiohttp.ClientSession() as session:
                params = {"limit": 100}
                data = await self._fetch_json(session, HF_DAILY_PAPERS_API, params=params)

                if not isinstance(data, list):
                    logger.warning("HFPapers: unexpected response format")
                    return []

                since_ts = effective_since.timestamp()

                for entry in data:
                    try:
                        paper = entry.get("paper", {})
                        paper_id = paper.get("id", "")
                        title = entry.get("title", "") or paper.get("title", "")
                        summary = entry.get("summary", "") or paper.get("summary", "")
                        authors = paper.get("authors", [])

                        # Parse published date
                        published_str = entry.get("publishedAt", "")
                        if published_str:
                            published_at = datetime.fromisoformat(
                                published_str.replace("Z", "+00:00")
                            )
                        else:
                            continue

                        if published_at.timestamp() < since_ts:
                            continue

                        # Author names
                        author_names = []
                        for a in authors[:5]:
                            if isinstance(a, dict):
                                name = a.get("name", "") or a.get("user", {}).get("fullname", "")
                            else:
                                name = str(a)
                            if name:
                                author_names.append(name)
                        submitted_by = entry.get("submittedBy", {})
                        if not author_names and isinstance(submitted_by, dict):
                            author_names = [submitted_by.get("fullname", "")]
                        author_str = ", ".join(author_names) if author_names else "Unknown"

                        # Upvotes as quality signal
                        upvotes = paper.get("upvotes", 0)
                        if not isinstance(upvotes, (int, float)):
                            upvotes = 0

                        # Truncate summary for storage
                        if len(summary) > 500:
                            summary = summary[:500] + "..."

                        url = f"https://huggingface.co/papers/{paper_id}" if paper_id else ""
                        arxiv_id = paper_id

                        items.append(RawItem(
                            source="hf_papers",
                            title=title,
                            url=url,
                            author=author_str,
                            published_at=published_at,
                            content=summary,
                            metadata={
                                "upvotes": upvotes,
                                "arxiv_id": arxiv_id,
                                "num_authors": len(authors),
                                "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
                            },
                        ))
                    except Exception as e:
                        logger.debug("HFPapers: failed to parse entry: %s", e)

        except Exception as e:
            logger.error("HFPapers collector failed: %s", e)

        logger.info("HFPapers: %d papers collected", len(items))
        return items
