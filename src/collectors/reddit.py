# src/collectors/reddit.py
import logging
import random
from datetime import datetime, timezone

import aiohttp

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

REDDIT_AUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE = "https://oauth.reddit.com"


class RedditCollector(BaseCollector):
    # Subreddits beyond this count get fewer sort orders to reduce request volume
    _FULL_SORT_THRESHOLD = 6

    def __init__(self, config: dict):
        super().__init__(config)
        reddit_cfg = config.get("sources", {}).get("reddit", {})
        self.client_id = reddit_cfg.get("client_id", "")
        self.client_secret = reddit_cfg.get("client_secret", "")
        self.subreddits = reddit_cfg.get("subreddits", [])
        self.min_score = reddit_cfg.get("min_score", 3)
        # Max subreddits to query per cycle; 0 = unlimited
        self.max_subreddits_per_cycle: int = reddit_cfg.get(
            "max_subreddits_per_cycle", 0
        )
        # Reddit public API: ~60 req/min, so 2s between requests is safe
        self.rate_limit_delay: float = 2.0
        # AI-focused subreddits: skip keyword filtering (everything is relevant)
        self._ai_subs = {
            "LocalLLaMA", "ClaudeAI", "ChatGPT", "ChatGPTCoding", "LangChain",
            "Rag", "ollama", "OpenAI", "Anthropic", "GoogleGeminiAI",
            "ArtificialIntelligence", "MLOps",
        }

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        data = {"grant_type": "client_credentials"}
        async with session.post(
            REDDIT_AUTH_URL, auth=auth, data=data,
            headers={"User-Agent": "AIteller/1.0"}
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            return result["access_token"]

    async def collect(self, since: datetime) -> list[RawItem]:
        items = []

        if self.client_id and self.client_secret and not self.client_id.startswith("${"):
            items = await self._collect_oauth(since)
        else:
            # Fallback: public JSON API (no auth needed)
            items = await self._collect_public(since)

        # Deduplicate by URL
        seen = set()
        unique = []
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        return unique

    def _pick_subreddits(self) -> list[str]:
        """Return subreddits for this cycle, sampling if over the cap."""
        subs = list(self.subreddits)
        if self.max_subreddits_per_cycle and len(subs) > self.max_subreddits_per_cycle:
            logger.info("Sampling %d/%d subreddits for this cycle",
                        self.max_subreddits_per_cycle, len(subs))
            subs = random.sample(subs, self.max_subreddits_per_cycle)
        return subs

    def _sort_orders_for(self, sub: str, sub_index: int) -> list[str]:
        """Return sort orders to query for a given subreddit.

        The first ``_FULL_SORT_THRESHOLD`` subreddits get all 4 sorts;
        the rest get only (hot, top) to reduce request volume.
        """
        if sub_index < self._FULL_SORT_THRESHOLD:
            return ["hot", "new", "top", "rising"]
        return ["hot", "top"]

    async def _collect_public(self, since: datetime) -> list[RawItem]:
        """Collect via Reddit's public JSON API (no OAuth required).

        Uses old.reddit.com (less restrictive than www.reddit.com) with
        browser-like headers to avoid 403 blocks.
        """
        items: list[RawItem] = []
        since_ts = since.timestamp()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        }
        subreddits = self._pick_subreddits()

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                for sub_idx, sub in enumerate(subreddits):
                    sub_count = 0
                    sort_orders = self._sort_orders_for(sub, sub_idx)
                    for sort in sort_orders:
                        try:
                            t_param = "&t=week" if sort == "top" else ""
                            url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit=100{t_param}"
                            data = await self._fetch_json(session, url)

                            for post in data.get("data", {}).get("children", []):
                                pd = post.get("data", {})
                                created = pd.get("created_utc", 0)
                                score = pd.get("score", 0)
                                if created < since_ts or score < self.min_score:
                                    continue
                                text = f"{pd.get('title', '')} {pd.get('selftext', '')}"
                                # AI-focused subs: skip keyword filter, let AI filter decide
                                if sub not in self._ai_subs and not self._matches_keywords(text):
                                    continue
                                items.append(RawItem(
                                    source="reddit",
                                    title=pd.get("title", ""),
                                    url=f"https://reddit.com{pd.get('permalink', '')}",
                                    author=pd.get("author", ""),
                                    published_at=datetime.fromtimestamp(created, tz=timezone.utc),
                                    content=pd.get("selftext", "")[:500],
                                    metadata={
                                        "score": score,
                                        "num_comments": pd.get("num_comments", 0),
                                        "subreddit": sub,
                                        "has_url": bool(pd.get("url_overridden_by_dest")),
                                        "link_url": pd.get("url_overridden_by_dest", ""),
                                        "upvote_ratio": pd.get("upvote_ratio", 0.0),
                                        "link_flair_text": pd.get("link_flair_text", ""),
                                        "is_self": pd.get("is_self", False),
                                        "domain": pd.get("domain", ""),
                                        "gilded": pd.get("gilded", 0),
                                    },
                                ))
                                sub_count += 1
                        except Exception as e:
                            logger.error("Reddit public API failed for r/%s/%s: %s", sub, sort, e)
                            continue
                    logger.info("r/%s: collected %d items (sorts: %s)",
                                sub, sub_count, ", ".join(sort_orders))
        except Exception as e:
            logger.error("Reddit collector (public) failed: %s", e)

        logger.info("Reddit public total: %d items from %d subreddits",
                     len(items), len(subreddits))
        return items

    async def _collect_oauth(self, since: datetime) -> list[RawItem]:
        """Collect via OAuth API (original method)."""
        items = []
        try:
            async with aiohttp.ClientSession() as session:
                token = await self._get_token(session)
                headers = {
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "AIteller/1.0",
                }
                since_ts = since.timestamp()

                for sub in self.subreddits:
                    try:
                        url = f"{REDDIT_API_BASE}/r/{sub}/hot?limit=100"
                        data = await self._fetch_json(session, url, headers=headers)
                        for post in data.get("data", {}).get("children", []):
                            pd = post.get("data", {})
                            created = pd.get("created_utc", 0)
                            score = pd.get("score", 0)
                            if created < since_ts or score < self.min_score:
                                continue
                            if not self._matches_keywords(
                                f"{pd.get('title', '')} {pd.get('selftext', '')}"
                            ):
                                continue
                            items.append(RawItem(
                                source="reddit",
                                title=pd.get("title", ""),
                                url=f"https://reddit.com{pd.get('permalink', '')}",
                                author=pd.get("author", ""),
                                published_at=datetime.fromtimestamp(created, tz=timezone.utc),
                                content=pd.get("selftext", "")[:500],
                                metadata={
                                    "score": score,
                                    "num_comments": pd.get("num_comments", 0),
                                    "subreddit": sub,
                                    "has_url": bool(pd.get("url_overridden_by_dest")),
                                    "link_url": pd.get("url_overridden_by_dest", ""),
                                    "upvote_ratio": pd.get("upvote_ratio", 0.0),
                                    "link_flair_text": pd.get("link_flair_text", ""),
                                    "is_self": pd.get("is_self", False),
                                    "domain": pd.get("domain", ""),
                                    "gilded": pd.get("gilded", 0),
                                },
                            ))
                    except Exception as e:
                        logger.error("Reddit failed for r/%s: %s", sub, e)
                        continue
        except Exception as e:
            logger.error("Reddit collector (oauth) failed: %s", e)

        return items
