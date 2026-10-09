# src/collectors/linkedin.py
"""Collector for LinkedIn articles via Google News RSS + LinkedIn newsletter feeds."""
import logging
import re
from datetime import datetime, timezone
from calendar import timegm
from urllib.parse import quote_plus

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

# Google News RSS search queries for LinkedIn AI content
_GOOGLE_NEWS_QUERIES = [
    "AI engineering practice site:linkedin.com",
    "LLM deployment site:linkedin.com",
    "AI agent site:linkedin.com",
    "AI LLM linkedin",
    "artificial intelligence linkedin article",
]

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en&when=7d"

# Title patterns that indicate job postings, not articles
_JOB_PATTERNS = re.compile(
    r"\bhiring\b|\bjob\b|\brecruit|\bposition\b|\bopening\b|"
    r"\bapply now\b|\bcareers?\b|\bjoin our team\b",
    re.IGNORECASE,
)


class LinkedInCollector(BaseCollector):
    def __init__(self, config: dict, feeds: dict = None):
        super().__init__(config)
        self.rate_limit_delay = 3.0  # be gentle with Google News
        self.newsletter_feeds = (feeds or {}).get("linkedin", [])
        source_cfg = config.get("sources", {}).get("linkedin", {})
        self.extra_queries = source_cfg.get("search_queries", [])

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []
        seen_urls = set()
        since_ts = since.timestamp()

        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (compatible; AIteller/1.0)"}
            ) as session:
                # Part 1: Google News RSS for LinkedIn articles
                all_queries = _GOOGLE_NEWS_QUERIES + self.extra_queries
                for query in all_queries:
                    try:
                        rss_url = _GOOGLE_NEWS_RSS.format(query=quote_plus(query))
                        text = await self._fetch_text(session, rss_url, max_retries=2)
                        feed = feedparser.parse(text)

                        for entry in feed.entries:
                            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
                            if not pub_time:
                                continue
                            entry_ts = timegm(pub_time)
                            if entry_ts < since_ts:
                                continue

                            entry_url = entry.get("link", "")
                            # Dedup by URL
                            if entry_url in seen_urls:
                                continue
                            seen_urls.add(entry_url)

                            title = entry.get("title", "")
                            # Google News appends " - Source Name" to titles
                            title = re.sub(r"\s*[-–]\s*(LinkedIn|CNBC|Forbes|Wired|ZDNET|Reuters)\s*$", "", title)

                            # Skip job postings
                            if _JOB_PATTERNS.search(title):
                                continue

                            # Source info from Google News entry
                            gn_source = ""
                            if hasattr(entry, "source") and isinstance(entry.source, dict):
                                gn_source = entry.source.get("title", "")

                            items.append(RawItem(
                                source="linkedin",
                                title=title,
                                url=entry_url,
                                author=gn_source or entry.get("author", ""),
                                published_at=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
                                content=entry.get("summary", "")[:500],
                                metadata={
                                    "search_query": query,
                                    "via": "google_news",
                                    "gn_source": gn_source,
                                },
                            ))
                    except Exception as e:
                        logger.warning("Google News query failed (%s): %s", query[:40], e)
                        continue

                # Part 2: Direct LinkedIn newsletter RSS feeds
                for feed_info in self.newsletter_feeds:
                    try:
                        name = feed_info.get("name", "LinkedIn Newsletter")
                        url = feed_info.get("url", "")
                        if not url:
                            continue

                        text = await self._fetch_text(session, url, max_retries=2)
                        feed = feedparser.parse(text)

                        for entry in feed.entries:
                            pub_time = entry.get("published_parsed") or entry.get("updated_parsed")
                            if not pub_time:
                                continue
                            entry_ts = timegm(pub_time)
                            if entry_ts < since_ts:
                                continue

                            entry_url = entry.get("link", "")
                            if entry_url in seen_urls:
                                continue
                            seen_urls.add(entry_url)

                            title = entry.get("title", "")
                            if not self._matches_keywords(f"{title} {entry.get('summary', '')}"):
                                continue

                            items.append(RawItem(
                                source="linkedin",
                                title=title,
                                url=entry_url,
                                author=entry.get("author", name),
                                published_at=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
                                content=entry.get("summary", "")[:500],
                                metadata={
                                    "newsletter_name": name,
                                    "via": "newsletter_rss",
                                },
                            ))
                    except Exception as e:
                        logger.warning("LinkedIn newsletter feed skipped for %s: %s", feed_info.get("name", "?"), e)
                        continue

        except Exception as e:
            logger.error("LinkedIn collector failed: %s", e)

        logger.info("LinkedInCollector total: %d items", len(items))
        return items
