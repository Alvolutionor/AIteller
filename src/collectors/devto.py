# src/collectors/devto.py
import logging
from calendar import timegm
from datetime import datetime, timezone

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

TAGS = ["ai", "machinelearning", "llm", "openai", "langchain", "rag", "python"]


class DevToCollector(BaseCollector):
    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 2.0

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []
        seen_urls: set[str] = set()
        since_ts = since.timestamp()

        try:
            async with aiohttp.ClientSession() as session:
                for tag in TAGS:
                    url = f"https://dev.to/feed/tag/{tag}"
                    try:
                        text = await self._fetch_text(session, url, max_retries=1)
                        feed = feedparser.parse(text)

                        for entry in feed.entries:
                            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
                            if not pub_time:
                                continue
                            entry_ts = timegm(pub_time)
                            if entry_ts < since_ts:
                                continue

                            entry_url = entry.get("link", "")
                            if not entry_url or entry_url in seen_urls:
                                continue

                            title = entry.get("title", "")
                            summary = entry.get("summary", "")
                            if not self._matches_keywords(f"{title} {summary}"):
                                continue

                            seen_urls.add(entry_url)
                            tags = [t.get("term", "") for t in entry.get("tags", [])]

                            items.append(RawItem(
                                source="devto",
                                title=title,
                                url=entry_url,
                                author=entry.get("author", ""),
                                published_at=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
                                content=summary[:500],
                                metadata={
                                    "tag": tag,
                                    "tags": tags,
                                },
                            ))
                    except Exception as e:
                        logger.warning("DevTo RSS feed skipped for tag '%s': %s", tag, e)
                        continue
        except Exception as e:
            logger.error("DevTo collector failed: %s", e)

        return items
