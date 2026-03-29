# src/collectors/twitter_api.py
"""Twitter collector using browser cookies (auth_token + ct0).

Anti-detection hardened: randomised delays, request budgets, UA rotation,
progressive backoff, query sampling, full browser-like headers.

How to get cookies:
  1. Log into x.com in Chrome
  2. Open DevTools (F12) → Application → Cookies → x.com
  3. Copy `auth_token` and `ct0` values
  4. Put them in config.yaml under sources.twitter.auth_token and sources.twitter.ct0
"""
import json
import logging
import random
import re
import uuid
from datetime import datetime, timezone
from html import unescape

import aiohttp
import asyncio

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL endpoints — query IDs are fetched dynamically at runtime
# ---------------------------------------------------------------------------
_GRAPHQL_BASE = "https://x.com/i/api/graphql"

# Fallback IDs (may be stale — dynamic fetch is preferred)
_FALLBACK_IDS = {
    "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
    "UserTweets": "FOlovQsiHGDls3c0Q_HaSQ",
    "SearchTimeline": "GcXk9vN_d1jUfHNqLacXQA",
}

# Cache for dynamically fetched IDs
_query_id_cache: dict[str, str] = {}


async def _fetch_query_ids() -> dict[str, str]:
    """Extract current GraphQL query IDs from x.com's JS bundles."""
    global _query_id_cache
    if _query_id_cache:
        return _query_id_cache

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
    }
    try:
        async with aiohttp.ClientSession(
            headers=headers, max_line_size=32768, max_field_size=32768
        ) as session:
            async with session.get("https://x.com",
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()

            js_urls = re.findall(
                r'https://abs\.twimg\.com/responsive-web/client-web[^"]+\.js', html
            )

            targets = {"SearchTimeline", "UserTweets", "UserByScreenName"}
            for url in js_urls[:15]:
                try:
                    async with session.get(url,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        js = await resp.text()
                except Exception:
                    continue

                for name in list(targets):
                    m = re.search(
                        rf'queryId:"([^"]+)",operationName:"{name}"', js
                    )
                    if m:
                        _query_id_cache[name] = m.group(1)
                        targets.discard(name)

                if not targets:
                    break

        logger.info("Twitter: fetched query IDs: %s", _query_id_cache)
    except Exception as exc:
        logger.warning("Twitter: failed to fetch query IDs: %s — using fallbacks", exc)

    # Fill missing with fallbacks
    for name, fallback in _FALLBACK_IDS.items():
        _query_id_cache.setdefault(name, fallback)

    return _query_id_cache

_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Full features dict matching last30days/Bird client (36 fields)
_FEATURES = {
    "rweb_video_screen_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_annotations_enabled": False,
    "responsive_web_jetfuel_frame": True,
    "post_ctas_fetch_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

# ---------------------------------------------------------------------------
# Fallback search queries — used only if config has no twitter_queries
# ---------------------------------------------------------------------------
_DEFAULT_SEARCH_QUERIES = [
    "AI agent production experience",
    "Claude Code tips OR tricks",
    "LLM RAG lessons learned",
]

# ---------------------------------------------------------------------------
# Realistic browser User-Agent pool
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# ---------------------------------------------------------------------------
# Budget defaults — configurable via config.yaml sources.twitter.*
# ---------------------------------------------------------------------------
_DEFAULT_MAX_REQUESTS = 40          # total API calls per collection cycle
_DEFAULT_MAX_SEARCH_QUERIES = 8     # randomly sample this many queries
_DEFAULT_MAX_USER_LOOKUPS = 15      # randomly sample this many accounts
_DEFAULT_DELAY_MIN = 6.0            # seconds
_DEFAULT_DELAY_MAX = 15.0           # seconds
_DEFAULT_RESULTS_PER_QUERY = 20     # tweets fetched per search


class TwitterAPICollector(BaseCollector):
    """Collect tweets using Twitter's GraphQL API with browser cookies.

    Anti-detection measures:
    - Randomised delay (6-15 s) with jitter between every request
    - Per-cycle request budget (default 40) — stops early when exhausted
    - Randomly samples a subset of search queries and user handles
    - Rotates User-Agent from a pool of current browser strings
    - Full browser-like request headers (Sec-Fetch-*, Accept, etc.)
    - Progressive backoff on non-200 responses
    - Reduces tweet count per query to look more like casual browsing
    """

    def __init__(self, config: dict, feeds: dict | None = None):
        super().__init__(config)
        tw_cfg = config.get("sources", {}).get("twitter", {})
        self.auth_token: str = tw_cfg.get("auth_token", "")
        self.ct0: str = tw_cfg.get("ct0", "")
        self.feeds: dict = feeds or {}

        # Search queries from config
        self._search_queries: list[str] = (
            config.get("keywords", {}).get("twitter_queries", [])
            or _DEFAULT_SEARCH_QUERIES
        )

        # Budget knobs
        self._max_requests = tw_cfg.get("max_requests", _DEFAULT_MAX_REQUESTS)
        self._max_search = tw_cfg.get("max_search_queries", _DEFAULT_MAX_SEARCH_QUERIES)
        self._max_users = tw_cfg.get("max_user_lookups", _DEFAULT_MAX_USER_LOOKUPS)
        self._delay_min = tw_cfg.get("delay_min", _DEFAULT_DELAY_MIN)
        self._delay_max = tw_cfg.get("delay_max", _DEFAULT_DELAY_MAX)
        self._results_per = tw_cfg.get("results_per_query", _DEFAULT_RESULTS_PER_QUERY)

        # Runtime counters
        self._request_count = 0
        self._consecutive_errors = 0
        self._ua = random.choice(_USER_AGENTS)

    # ------------------------------------------------------------------
    # Delay & budget helpers
    # ------------------------------------------------------------------

    def _budget_ok(self) -> bool:
        if self._max_requests <= 0:
            return True  # unlimited
        return self._request_count < self._max_requests

    async def _human_delay(self):
        """Sleep a randomised interval that looks like a human browsing."""
        base = random.uniform(self._delay_min, self._delay_max)
        # Occasional longer pause (10% chance) to simulate reading
        if random.random() < 0.10:
            base += random.uniform(8.0, 20.0)
        await asyncio.sleep(base)

    async def _backoff_delay(self):
        """Exponential backoff on repeated errors."""
        wait = min(30 * (2 ** self._consecutive_errors), 300)
        jitter = random.uniform(0, wait * 0.3)
        logger.info("Twitter backoff: %.0fs (errors=%d)", wait + jitter, self._consecutive_errors)
        await asyncio.sleep(wait + jitter)

    # ------------------------------------------------------------------
    # Headers — mimic a real Chrome session
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict:
        """Bird-client compatible headers with per-request unique IDs."""
        return {
            "Authorization": f"Bearer {_BEARER_TOKEN}",
            "X-Csrf-Token": self.ct0,
            "Cookie": f"auth_token={self.auth_token}; ct0={self.ct0}",
            "User-Agent": self._ua,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Client-Language": "en",
            "X-Client-Uuid": str(uuid.uuid4()),
            "X-Twitter-Client-Deviceid": str(uuid.uuid4()),
            "X-Client-Transaction-Id": uuid.uuid4().hex,
        }

    # ------------------------------------------------------------------
    # Safe request wrapper
    # ------------------------------------------------------------------

    async def _api_post(self, session: aiohttp.ClientSession, url: str,
                        variables: dict, query_id: str) -> dict | None:
        """GraphQL POST request (used by SearchTimeline, like Bird client)."""
        if not self._budget_ok():
            return None

        await self._human_delay()
        self._request_count += 1

        params = {"variables": json.dumps(variables)}
        body = {"features": _FEATURES, "queryId": query_id}

        try:
            async with session.post(
                url, params=params, json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    self._consecutive_errors = 0
                    return await resp.json()

                if resp.status == 429:
                    self._consecutive_errors += 1
                    logger.warning("Twitter 429 rate limited (req #%d)", self._request_count)
                    await self._backoff_delay()
                    return None

                if resp.status in (401, 403):
                    text = await resp.text()
                    logger.error("Twitter auth error %s: %s", resp.status, text[:200])
                    self._request_count = self._max_requests
                    return None

                self._consecutive_errors += 1
                logger.warning("Twitter POST HTTP %s (req #%d)",
                               resp.status, self._request_count)
                if self._consecutive_errors >= 3:
                    await self._backoff_delay()
                return None

        except asyncio.TimeoutError:
            self._consecutive_errors += 1
            logger.warning("Twitter POST timeout (req #%d)", self._request_count)
            return None
        except Exception as exc:
            self._consecutive_errors += 1
            logger.error("Twitter POST error: %s", exc)
            return None

    async def _api_get(self, session: aiohttp.ClientSession, url: str,
                       params: dict) -> dict | None:
        """Single GraphQL GET with budget tracking and error handling."""
        if not self._budget_ok():
            logger.info("Twitter request budget exhausted (%d/%d)",
                        self._request_count, self._max_requests)
            return None

        await self._human_delay()
        self._request_count += 1

        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    self._consecutive_errors = 0
                    return await resp.json()

                if resp.status == 429:
                    self._consecutive_errors += 1
                    logger.warning("Twitter 429 rate limited (req #%d)", self._request_count)
                    await self._backoff_delay()
                    return None

                if resp.status in (401, 403):
                    text = await resp.text()
                    logger.error("Twitter auth error %s: %s", resp.status, text[:200])
                    # Auth failure — stop everything this cycle
                    self._request_count = self._max_requests
                    return None

                # Other errors
                self._consecutive_errors += 1
                text = await resp.text()
                logger.warning("Twitter HTTP %s (req #%d): %s",
                               resp.status, self._request_count, text[:200])
                if self._consecutive_errors >= 3:
                    await self._backoff_delay()
                return None

        except asyncio.TimeoutError:
            self._consecutive_errors += 1
            logger.warning("Twitter request timeout (req #%d)", self._request_count)
            return None
        except Exception as exc:
            self._consecutive_errors += 1
            logger.error("Twitter request error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def collect(self, since: datetime) -> list[RawItem]:
        if not self.auth_token or not self.ct0:
            logger.warning("TwitterAPICollector: no auth_token/ct0 configured, skipping")
            return []

        # Fetch fresh query IDs before starting
        ids = await _fetch_query_ids()
        self._ep_search = f"{ids['SearchTimeline']}/SearchTimeline"
        self._ep_tweets = f"{ids['UserTweets']}/UserTweets"
        self._ep_user = f"{ids['UserByScreenName']}/UserByScreenName"

        # Reset per-cycle state
        self._request_count = 0
        self._consecutive_errors = 0
        self._ua = random.choice(_USER_AGENTS)

        items: list[RawItem] = []
        headers = self._get_headers()

        async with aiohttp.ClientSession(headers=headers) as session:
            # --- Strategy 1: Sample user accounts ---
            handles = self.feeds.get("twitter", [])
            if handles:
                n_users = len(handles) if self._max_users <= 0 else min(len(handles), self._max_users)
                sampled = random.sample(handles, n_users)
                random.shuffle(sampled)
                logger.info("Twitter: sampling %d/%d accounts", len(sampled), len(handles))

                for account in sampled:
                    if not self._budget_ok():
                        break
                    handle = account.get("handle", "")
                    if not handle:
                        continue
                    batch = await self._collect_user(session, handle, since)
                    items.extend(batch)
                    if batch:
                        logger.info("Twitter @%s: %d tweets", handle, len(batch))

            # --- Strategy 2: Sample search queries ---
            if self._budget_ok():
                n_queries = len(self._search_queries) if self._max_search <= 0 else min(len(self._search_queries), self._max_search)
                sampled_queries = random.sample(self._search_queries, n_queries)
                random.shuffle(sampled_queries)
                logger.info("Twitter: sampling %d/%d search queries",
                            len(sampled_queries), len(self._search_queries))

                for query in sampled_queries:
                    if not self._budget_ok():
                        break
                    batch = await self._search_tweets(session, query, since)
                    items.extend(batch)
                    if batch:
                        logger.info("Twitter search '%s': %d tweets", query, len(batch))

        logger.info("Twitter collection done: %d items, %d/%d requests used",
                     len(items), self._request_count, self._max_requests)

        # Dedup by tweet URL
        seen: set[str] = set()
        return [i for i in items if i.url not in seen and not seen.add(i.url)]

    # ------------------------------------------------------------------
    # User timeline
    # ------------------------------------------------------------------

    async def _collect_user(self, session, handle: str, since: datetime) -> list[RawItem]:
        user_id = await self._get_user_id(session, handle)
        if not user_id:
            return []

        variables = {
            "userId": user_id,
            "count": self._results_per,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": False,
            "withV2Timeline": True,
        }
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(_FEATURES),
        }

        data = await self._api_get(session, f"{_GRAPHQL_BASE}/{self._ep_tweets}", params)
        if not data:
            return []

        return self._parse_user_tweets(data, handle, since)

    def _parse_user_tweets(self, data: dict, handle: str, since: datetime) -> list[RawItem]:
        items = []
        since_ts = since.timestamp()

        instructions = (data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                        .get("timeline_v2", {})
                        .get("timeline", {})
                        .get("instructions", []))

        for instruction in instructions:
            for entry in instruction.get("entries", []):
                tweet = self._extract_tweet(entry, since_ts)
                if tweet:
                    tweet.metadata["handle"] = handle
                    tweet.metadata["strategy_used"] = "graphql_api"
                    tweet.author = handle
                    items.append(tweet)
        return items

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def _search_tweets(self, session, query: str, since: datetime) -> list[RawItem]:
        """Search via POST (Bird-client compatible)."""
        variables = {
            "rawQuery": query + " -filter:retweets",
            "count": self._results_per,
            "querySource": "typed_query",
            "product": "Latest",
        }

        # Extract query ID from endpoint path
        query_id = self._ep_search.split("/")[0]
        url = f"{_GRAPHQL_BASE}/{self._ep_search}"

        data = await self._api_post(session, url, variables, query_id)
        if not data:
            return []

        return self._parse_search_tweets(data, query, since)

    def _parse_search_tweets(self, data: dict, query: str, since: datetime) -> list[RawItem]:
        items = []
        since_ts = since.timestamp()

        instructions = (data.get("data", {})
                        .get("search_by_raw_query", {})
                        .get("search_timeline", {})
                        .get("timeline", {})
                        .get("instructions", []))

        for instruction in instructions:
            for entry in instruction.get("entries", []):
                tweet = self._extract_tweet(entry, since_ts)
                if tweet:
                    tweet.metadata["search_query"] = query
                    tweet.metadata["strategy_used"] = "graphql_search"
                    items.append(tweet)
        return items

    # ------------------------------------------------------------------
    # Shared tweet extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tweet(entry: dict, since_ts: float) -> RawItem | None:
        """Extract a single tweet from a timeline entry. Returns None if invalid."""
        try:
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            tweet_result = item_content.get("tweet_results", {}).get("result", {})
            if not tweet_result:
                return None
            if tweet_result.get("__typename") == "TweetWithVisibilityResults":
                tweet_result = tweet_result.get("tweet", {})

            legacy = tweet_result.get("legacy", {})
            if not legacy:
                return None

            # Author
            core = tweet_result.get("core", {}).get("user_results", {}).get("result", {})
            handle = core.get("legacy", {}).get("screen_name", "unknown")

            # Timestamp
            created_str = legacy.get("created_at", "")
            if not created_str:
                return None
            published_at = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
            if published_at.timestamp() < since_ts:
                return None

            # Text
            full_text = unescape(legacy.get("full_text", ""))
            if full_text.startswith("RT @"):
                return None

            tweet_id = legacy.get("id_str", "")
            tweet_url = f"https://x.com/{handle}/status/{tweet_id}"

            # Metrics
            likes = legacy.get("favorite_count", 0)
            retweets = legacy.get("retweet_count", 0)
            replies = legacy.get("reply_count", 0)
            views_raw = tweet_result.get("views", {}).get("count", "0")
            try:
                views = int(views_raw)
            except (ValueError, TypeError):
                views = 0

            urls = [
                u.get("expanded_url", "")
                for u in legacy.get("entities", {}).get("urls", [])
                if u.get("expanded_url")
            ]

            # Additional metrics
            quotes = legacy.get("quote_count", 0)
            bookmarks = legacy.get("bookmark_count", 0)

            # Detect thread: tweet is a self-reply (reply to same author)
            in_reply_to_user = legacy.get("in_reply_to_screen_name", "")
            is_thread = (in_reply_to_user.lower() == handle.lower()) if in_reply_to_user else False

            # Detect media (images/video)
            has_media = bool(legacy.get("entities", {}).get("media"))

            # Tweet language
            lang = legacy.get("lang", "")

            title = full_text.split("\n")[0][:100]
            if len(title) < len(full_text.split("\n")[0]):
                title += "..."

            return RawItem(
                source="twitter",
                title=title,
                url=tweet_url,
                author=handle,
                published_at=published_at,
                content=full_text,
                metadata={
                    "handle": handle,
                    "likes": likes,
                    "retweets": retweets,
                    "replies": replies,
                    "views": views,
                    "urls": urls,
                    "quote_count": quotes,
                    "bookmark_count": bookmarks,
                    "is_thread": is_thread,
                    "has_media": has_media,
                    "lang": lang,
                },
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # User lookup (with caching)
    # ------------------------------------------------------------------

    _user_id_cache: dict[str, str] = {}

    async def _get_user_id(self, session, handle: str) -> str | None:
        if handle in self._user_id_cache:
            return self._user_id_cache[handle]

        variables = {
            "screen_name": handle,
            "withSafetyModeUserFields": True,
        }
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(_FEATURES),
        }

        data = await self._api_get(session, f"{_GRAPHQL_BASE}/{self._ep_user}", params)
        if not data:
            return None

        try:
            uid = data["data"]["user"]["result"]["rest_id"]
            self._user_id_cache[handle] = uid
            return uid
        except (KeyError, TypeError):
            logger.error("Twitter user lookup @%s: couldn't extract user ID", handle)
            return None
