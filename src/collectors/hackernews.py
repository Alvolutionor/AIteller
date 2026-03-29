# src/collectors/hackernews.py
"""HackerNews collector via Algolia API.

Three strategies:
  1. Keyword search across story/show_hn/ask_hn
  2. Popularity search (by points) for trending AI posts
  3. Broad browse of recent Show/Ask HN (keyword filter client-side)

For high-comment posts, fetches top comments to enrich content for AI filtering.
"""
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

ALGOLIA_SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"
ALGOLIA_SEARCH_POPULAR = "https://hn.algolia.com/api/v1/search"
ALGOLIA_ITEM_API = "https://hn.algolia.com/api/v1/items"

# Posts with this many comments get their top comments fetched
_COMMENTS_FETCH_THRESHOLD = 5


class HackerNewsCollector(BaseCollector):
    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 2.0
        hn_cfg = config.get("sources", {}).get("hackernews", {})
        self.min_points = hn_cfg.get("min_points", 10)

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []
        keywords = self.config.get("keywords", {}).get("primary", [])
        timestamp = int(since.timestamp())

        try:
            async with aiohttp.ClientSession() as session:
                # Strategy 1: keyword search — all keywords, all tag types
                for tag_type in ("story", "show_hn", "ask_hn"):
                    min_pts = self.min_points if tag_type == "story" else 1
                    for kw in keywords:
                        batch = await self._search(session, kw, tag_type, timestamp, min_pts)
                        items.extend(batch)

                # Strategy 2: popularity search — core AI terms
                for kw in ["AI", "LLM", "GPT", "Claude", "agent", "RAG", "coding agent",
                            "fine-tuning", "prompt engineering", "AI coding"]:
                    batch = await self._search_popular(session, kw, timestamp)
                    items.extend(batch)

                # Strategy 3: broad browse — recent Show/Ask HN, no keyword requirement
                for tag_type in ("show_hn", "ask_hn"):
                    batch = await self._browse_recent(session, tag_type, timestamp)
                    items.extend(batch)

                # Deduplicate by HN ID first (before expensive comment fetching)
                seen_ids = set()
                unique = []
                for item in items:
                    hn_id = item.metadata.get("hn_id", "")
                    if hn_id not in seen_ids:
                        seen_ids.add(hn_id)
                        unique.append(item)
                items = unique

                # Enrich high-discussion posts with top comments
                enriched = 0
                consecutive_errors = 0
                for item in items:
                    num_comments = item.metadata.get("comments", 0)
                    if num_comments >= _COMMENTS_FETCH_THRESHOLD:
                        try:
                            comment_text, top_comment_texts = await self._fetch_top_comments(
                                session, item.metadata["hn_id"], max_comments=5
                            )
                            if comment_text:
                                item.content = (item.content + "\n\n--- Top HN Comments ---\n" + comment_text).strip()
                                enriched += 1
                            if top_comment_texts:
                                item.metadata["top_comments"] = top_comment_texts
                            consecutive_errors = 0
                        except Exception as e:
                            consecutive_errors += 1
                            logger.debug("HN comment fetch error for %s: %s", item.metadata.get("hn_id"), e)
                            if consecutive_errors >= 5:
                                logger.warning("HackerNews: %d consecutive errors enriching comments, stopping early", consecutive_errors)
                                break

                logger.info("HackerNews: enriched %d/%d posts with comments", enriched, len(items))

        except Exception as e:
            logger.error("HackerNews collector failed: %s", e)

        # Final dedup by URL
        seen_urls = set()
        final = []
        for item in items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                final.append(item)

        logger.info("HackerNews: %d unique items", len(final))
        return final

    async def _search(self, session, keyword, tag_type, timestamp, min_points) -> list[RawItem]:
        qs = urlencode({
            "query": keyword,
            "tags": tag_type,
            "numericFilters": f"created_at_i>{timestamp}",
            "hitsPerPage": 100,
        })
        try:
            data = await self._fetch_json(session, f"{ALGOLIA_SEARCH_API}?{qs}")
            return self._parse_hits(data.get("hits", []), min_points, tag_type)
        except Exception as e:
            logger.debug("HN search failed: tag=%s kw='%s': %s", tag_type, keyword, e)
            return []

    async def _search_popular(self, session, keyword, timestamp) -> list[RawItem]:
        qs = urlencode({
            "query": keyword,
            "tags": "story",
            "numericFilters": f"created_at_i>{timestamp},points>15",
            "hitsPerPage": 50,
        })
        try:
            data = await self._fetch_json(session, f"{ALGOLIA_SEARCH_POPULAR}?{qs}")
            return self._parse_hits(data.get("hits", []), 15, "story")
        except Exception as e:
            logger.debug("HN popular search failed for '%s': %s", keyword, e)
            return []

    async def _browse_recent(self, session, tag_type, timestamp) -> list[RawItem]:
        """Browse recent Show/Ask HN broadly — let AI filter handle relevance."""
        qs = urlencode({
            "tags": tag_type,
            "numericFilters": f"created_at_i>{timestamp}",
            "hitsPerPage": 500,
        })
        try:
            data = await self._fetch_json(session, f"{ALGOLIA_SEARCH_API}?{qs}")
            hits = data.get("hits", [])
            items = []
            for hit in hits:
                points = hit.get("points") or 0
                if points < 1:
                    continue
                title = hit.get("title", "")
                text = hit.get("story_text", "")
                # Broad keyword match — any AI-related term
                if self._matches_keywords(f"{title} {text}"):
                    items.append(self._hit_to_item(hit, tag_type))
            return items
        except Exception as e:
            logger.debug("HN browse failed for %s: %s", tag_type, e)
            return []

    async def _fetch_top_comments(self, session, hn_id: str, max_comments: int = 5) -> tuple[str, list[str]]:
        """Fetch top-level comments for a HN post.

        Returns (concatenated_text, top_3_comment_texts) where the second
        element contains the plain-text of the top 3 comments for metadata.
        """
        try:
            url = f"{ALGOLIA_ITEM_API}/{hn_id}"
            data = await self._fetch_json(session, url)
            children = data.get("children", [])
            # Sort by points (highest first), take top N
            children.sort(key=lambda c: c.get("points") or 0, reverse=True)
            comments = []
            plain_texts: list[str] = []
            for child in children[:max_comments]:
                text = child.get("text", "")
                author = child.get("author", "")
                points = child.get("points") or 0
                if text and len(text) > 20:
                    # Strip HTML tags
                    import re
                    clean = re.sub(r"<[^>]+>", " ", text).strip()
                    clean = re.sub(r"\s+", " ", clean)
                    if len(clean) > 500:
                        clean = clean[:500] + "..."
                    comments.append(f"[{author}, {points}pts]: {clean}")
                    if len(plain_texts) < 3:
                        plain_texts.append(clean)
            return ("\n".join(comments) if comments else "", plain_texts)
        except Exception as e:
            logger.debug("HN comments fetch failed for %s: %s", hn_id, e)
            return ("", [])

    def _parse_hits(self, hits, min_points, tag_type) -> list[RawItem]:
        items = []
        for hit in hits:
            points = hit.get("points") or 0
            if points < min_points:
                continue
            items.append(self._hit_to_item(hit, tag_type))
        return items

    def _hit_to_item(self, hit, tag_type) -> RawItem:
        article_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        # Derive type from _tags if available (more accurate than search tag_type)
        tags = hit.get("_tags") or []
        hn_type = tag_type
        for t in ("show_hn", "ask_hn", "job"):
            if t in tags:
                hn_type = t
                break
        return RawItem(
            source="hackernews",
            title=hit.get("title", ""),
            url=article_url,
            author=hit.get("author", ""),
            published_at=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
            content=hit.get("story_text") or "",
            metadata={
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments", 0),
                "descendants": hit.get("num_comments", 0),
                "hn_id": hit["objectID"],
                "hn_type": hn_type,
                "top_comments": [],
            },
        )
