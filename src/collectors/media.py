# src/collectors/media.py
"""Collector for tech media RSS feeds (TechCrunch, VentureBeat, The Verge, etc.)."""
import logging
from datetime import datetime, timezone
from calendar import timegm

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)


class MediaCollector(BaseCollector):
    def __init__(self, config: dict, feeds: dict = None):
        super().__init__(config)
        self.rate_limit_delay = 2.0
        self.media_feeds = (feeds or {}).get("media", [])

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []
        since_ts = since.timestamp()

        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": "AIteller/1.0 (+https://github.com/aiteller)"}
            ) as session:
                for feed_info in self.media_feeds:
                    try:
                        name = feed_info.get("name", "Unknown Media")
                        url = feed_info.get("url", "")
                        if not url:
                            continue

                        text = await self._fetch_text(session, url, max_retries=2)
                        feed = feedparser.parse(text)

                        count = 0
                        for entry in feed.entries:
                            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
                            if not pub_time:
                                continue
                            entry_ts = timegm(pub_time)
                            if entry_ts < since_ts:
                                continue

                            title = entry.get("title", "")
                            summary = entry.get("summary", "")
                            if not self._matches_keywords(f"{title} {summary}"):
                                continue

                            entry_url = entry.get("link", "")
                            author = entry.get("author", "")
                            tags = [t.get("term", "") for t in entry.get("tags", [])]

                            items.append(RawItem(
                                source="media",
                                title=title,
                                url=entry_url,
                                author=author or name,
                                published_at=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
                                content=summary[:800],
                                metadata={
                                    "media_name": name,
                                    "tags": tags,
                                },
                            ))
                            count += 1

                        if count > 0:
                            logger.info("Media %s: %d items", name, count)
                    except Exception as e:
                        logger.warning("Media feed skipped for %s: %s", feed_info.get("name", "?"), e)
                        continue
        except Exception as e:
            logger.error("Media collector failed: %s", e)

        logger.info("MediaCollector total: %d items", len(items))
        return items
