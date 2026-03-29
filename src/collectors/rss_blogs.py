# src/collectors/rss_blogs.py
import logging
from datetime import datetime, timezone
from calendar import timegm

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)


class RSSBlogsCollector(BaseCollector):
    def __init__(self, config: dict, feeds: dict = None):
        super().__init__(config)
        self.rate_limit_delay = 1.5
        self.blogs = (feeds or {}).get("blogs", [])

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []
        since_ts = since.timestamp()

        try:
            async with aiohttp.ClientSession() as session:
                for blog in self.blogs:
                    try:
                        name = blog.get("name", "Unknown")
                        url = blog.get("url", "")
                        if not url:
                            continue
                        text = await self._fetch_text(session, url, max_retries=1)
                        feed = feedparser.parse(text)

                        for entry in feed.entries:
                            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
                            if not pub_time:
                                continue
                            entry_ts = timegm(pub_time)
                            if entry_ts < since_ts:
                                continue

                            title = entry.get("title", "")
                            if not self._matches_keywords(f"{title} {entry.get('summary', '')}"):
                                continue

                            entry_url = entry.get("link", "")
                            tags = [t.get("term", "") for t in entry.get("tags", [])]

                            items.append(RawItem(
                                source="rss_blogs",
                                title=title,
                                url=entry_url,
                                author=entry.get("author", name),
                                published_at=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
                                content=entry.get("summary", "")[:500],
                                metadata={
                                    "blog_name": name,
                                    "tags": tags,
                                },
                            ))
                    except Exception as e:
                        logger.warning("RSS feed skipped for %s: %s", blog.get("name", "?"), e)
                        continue
        except Exception as e:
            logger.error("RSS Blogs collector failed: %s", e)

        return items
