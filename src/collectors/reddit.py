# src/collectors/reddit.py
import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import feedparser

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

REDDIT_AUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE = "https://oauth.reddit.com"

# Control chars that are illegal in XML 1.0 and occasionally appear in
# Reddit RSS post bodies, breaking the SAX parser. Stripped before parse.
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class _RedditProgress:
    """Per-subreddit collection checkpoint persisted to a JSON file.

    Lets a Reddit collect run survive being killed: each subreddit is
    marked ``done`` the moment its items are collected and inserted, so a
    re-run with the same checkpoint path skips finished subs and never
    re-issues their requests (the whole point — re-firing requests is what
    risks the 429 / IP ban). Subs left ``pending``/``failed`` are retried.

    File shape::

        {
          "created_at": "2026-06-14T...",
          "subs": {"LocalLLaMA": {"status": "done", "collected": 42,
                                  "updated_at": "..."}, ...}
        }
    """

    def __init__(self, path: Path, valid_after: str | None = None):
        self.path = Path(path)
        self.data: dict = {"created_at": None, "subs": {}}
        self._load(valid_after)

    def _load(self, valid_after: str | None) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("subs", {})
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Reddit checkpoint unreadable (%s) — starting fresh", e)
                self.data = {"created_at": None, "subs": {}}
                return
            # Expire a stale checkpoint. A checkpoint only exists to resume a
            # run that was interrupted minutes/hours ago; anything older is a
            # previous cycle whose "done" marks must not suppress this one.
            # valid_after is a freshness cutoff (default: 6h ago) — created
            # before it → reset. (Old behavior keyed this off the content
            # window start, which failed when a new cycle started sooner than
            # the window length and skipped every sub.)
            created = self.data.get("created_at")
            if valid_after and created and created < valid_after:
                logger.info("Reddit checkpoint stale (created %s < cutoff %s) "
                            "— resetting for new cycle", created, valid_after)
                self.data = {"created_at": None, "subs": {}}

    def _save(self, now_iso: str) -> None:
        if self.data.get("created_at") is None:
            self.data["created_at"] = now_iso
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)  # atomic on same filesystem
        except OSError as e:
            logger.warning("Reddit checkpoint write failed (%s)", e)

    def is_done(self, sub: str) -> bool:
        return self.data["subs"].get(sub, {}).get("status") == "done"

    def mark(self, sub: str, status: str, collected: int, now_iso: str) -> None:
        self.data["subs"][sub] = {
            "status": status,
            "collected": collected,
            "updated_at": now_iso,
        }
        self._save(now_iso)

    def done_count(self) -> int:
        return sum(1 for v in self.data["subs"].values()
                   if v.get("status") == "done")


def _random_ios_ua() -> str:
    """Return a randomized iOS mobile Safari User-Agent.

    Reddit hard-blocks desktop-browser and generic User-Agents on its
    ``.json`` endpoints (HTTP 403) as of 2026, but mobile Safari UAs are
    treated more leniently. Technique adapted from ecklf/reddit-clawler;
    randomizing the version components avoids a static fingerprint.
    """
    ios_major = random.randint(15, 18)
    ios_minor = random.randint(0, 6)
    webkit = random.randint(600, 615)
    safari = random.randint(600, 605)
    return (
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_major}_{ios_minor} like Mac OS X) "
        f"AppleWebKit/{webkit}.1.15 (KHTML, like Gecko) Version/{safari}.0 "
        f"Mobile/15E148 Safari/{webkit}.1"
    )


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
        # Reddit public API gates aggressively when bursts hit it. Pace
        # very conservatively — better slow than IP-banned for the rest
        # of the run. Tune via config if needed.
        self.rate_limit_delay: float = float(reddit_cfg.get("rate_limit_delay", 8.0))
        # Inter-sort jitter (per subreddit, between sort orders)
        self._inter_sort_min = float(reddit_cfg.get("inter_sort_min", 3.0))
        self._inter_sort_max = float(reddit_cfg.get("inter_sort_max", 7.0))
        # Inter-subreddit jitter (between subreddits)
        self._inter_sub_min = float(reddit_cfg.get("inter_sub_min", 8.0))
        self._inter_sub_max = float(reddit_cfg.get("inter_sub_max", 15.0))
        # Long pause every N subreddits to mimic human break
        self._long_pause_every = int(reddit_cfg.get("long_pause_every", 5))
        self._long_pause_min = float(reddit_cfg.get("long_pause_min", 30.0))
        self._long_pause_max = float(reddit_cfg.get("long_pause_max", 60.0))
        # Trip the circuit breaker after N consecutive 403s
        self._cb_threshold = int(reddit_cfg.get("circuit_breaker_after", 3))
        # RSS fetch retry budget — higher than the base default of 3 so a
        # transient 429 burst clears (with Retry-After + backoff) instead
        # of permanently dropping a sort.
        self._rss_max_retries = int(reddit_cfg.get("rss_max_retries", 5))
        # Per-sub progress checkpoint. Default lives next to the DB so it
        # is co-located with the run's state. A run resumed with the same
        # file skips subs already marked done and never re-fires their
        # requests. Set to "" / null in config to disable checkpointing.
        ckpt = reddit_cfg.get("progress_file", None)
        if ckpt is None:
            db_path = config.get("storage", {}).get("db_path", "./data/aiteller.db")
            ckpt = str(Path(db_path).parent / "reddit_progress.json")
        self._progress_path = Path(ckpt) if ckpt else None
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

        We now collect primarily via Reddit's RSS feeds (the ``.json``
        endpoints are IP-blocked with HTTP 403 as of 2026), and RSS is
        not subject to the per-IP burst throttling that the old
        single-sort policy was guarding against. That lets us restore
        ``top`` (t=week) alongside ``hot`` for better weekly coverage:
        ``hot`` captures what's surfacing right now, ``top`` captures the
        week's highest-signal posts that may have already cooled off.
        """
        return ["hot", "top"]

    async def _collect_public(self, since: datetime) -> list[RawItem]:
        """Collect via Reddit's public endpoints (no OAuth required).

        Strategy (2026): the ``.json`` endpoints are hard-blocked with
        HTTP 403 from many IPs regardless of pacing or host. We therefore
        try ``.json`` first with a randomized iOS-Safari User-Agent (gives
        full metadata — score, comments, flair), and once it proves
        blocked we switch to the subreddit **RSS feed**, which is not
        subject to the same block and reliably returns 200. RSS loses the
        engagement metadata but keeps title/author/date/permalink/body,
        which is enough for the AI filter and the rest of the scorer.
        """
        items: list[RawItem] = []
        since_ts = since.timestamp()

        # Pacing: we collect primarily via RSS. Reddit still throttles RSS
        # with HTTP 429 when requests come too fast, so pace conservatively
        # (slow-but-unbanned). Honor the configured inter-sub / inter-sort
        # jitter and the periodic long pause instead of compressing them —
        # the previous "finish in a few minutes" compression is exactly
        # what tripped the 429 storm. ~8-15s between subs by default.
        inter_sub_min = self._inter_sub_min
        inter_sub_max = self._inter_sub_max

        headers = {
            "User-Agent": _random_ios_ua(),
            "Accept": "application/json, text/html;q=0.9, application/atom+xml;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        subreddits = self._pick_subreddits()
        json_host_candidates = ["old.reddit.com", "www.reddit.com"]
        good_json_host: str | None = None
        json_disabled = False
        json_attempts = 0
        consecutive_fail = 0
        circuit_open = False

        # Resume support: skip subs already marked done in the checkpoint,
        # and persist each sub's status the moment it finishes so a kill
        # mid-run never re-issues a completed sub's requests on resume.
        # Staleness is judged against a freshness cutoff (checkpoint_ttl_hours,
        # default 6h): a checkpoint older than that belongs to a previous
        # cycle and is reset, so Reddit can run inline in the normal collect
        # without the old "delete reddit_progress.json first" workaround.
        now_iso = datetime.now(timezone.utc).isoformat()
        ttl_h = self.config.get("sources", {}).get("reddit", {}).get("checkpoint_ttl_hours", 6)
        fresh_cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_h)).isoformat()
        progress = (_RedditProgress(self._progress_path, valid_after=fresh_cutoff)
                    if self._progress_path else None)
        if progress:
            already = progress.done_count()
            if already:
                logger.info("Reddit checkpoint: skipping %d sub(s) already done "
                            "(file: %s)", already, self._progress_path)

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                first = True
                for sub_idx, sub in enumerate(subreddits):
                    if circuit_open:
                        logger.info("r/%s: skipped (circuit breaker open)", sub)
                        continue
                    if progress and progress.is_done(sub):
                        logger.info("r/%s: skipped (checkpoint: done)", sub)
                        continue
                    # Long human-like pause every N processed subs
                    if not first:
                        if (self._long_pause_every and sub_idx > 0
                                and sub_idx % self._long_pause_every == 0):
                            pause = random.uniform(self._long_pause_min, self._long_pause_max)
                            logger.info("Reddit: long pause %.0fs (anti-throttle)", pause)
                            await asyncio.sleep(pause)
                        else:
                            await asyncio.sleep(random.uniform(inter_sub_min, inter_sub_max))
                    first = False

                    sub_count = 0
                    sort_orders = self._sort_orders_for(sub, sub_idx)
                    sub_failed = False
                    for sort_idx, sort in enumerate(sort_orders):
                        if circuit_open:
                            break
                        # Space out the sort requests within a sub too.
                        if sort_idx > 0:
                            await asyncio.sleep(
                                random.uniform(self._inter_sort_min, self._inter_sort_max))
                        new_items: list[RawItem] | None = None

                        # 1) Try .json (richer metadata) unless proven blocked
                        if not json_disabled:
                            t_param = "&t=week" if sort == "top" else ""
                            hosts = [good_json_host] if good_json_host else json_host_candidates
                            for host in hosts:
                                url = (f"https://{host}/r/{sub}/{sort}.json"
                                       f"?limit=100&raw_json=1{t_param}")
                                try:
                                    data = await self._fetch_json(session, url, loose_mime=True)
                                    good_json_host = host
                                    new_items = self._parse_json(data, sub, since_ts)
                                    break
                                except Exception:
                                    continue
                            json_attempts += 1
                            # If .json never once succeeded after a couple of
                            # probes, it's IP-blocked — stop hammering it and
                            # go RSS-only for the rest of the run.
                            if new_items is None and good_json_host is None and json_attempts >= 2:
                                json_disabled = True
                                logger.info(
                                    "Reddit: .json blocked (HTTP 403) — switching to "
                                    "RSS-only for the rest of this run")

                        # 2) Fall back to RSS when .json is blocked / failed
                        if new_items is None:
                            try:
                                new_items = await self._fetch_rss(session, sub, sort, since_ts)
                            except Exception as e:
                                logger.warning("Reddit RSS failed for r/%s/%s: %s", sub, sort, e)
                                new_items = None

                        if new_items is None:
                            sub_failed = True
                            consecutive_fail += 1
                            if consecutive_fail >= self._cb_threshold:
                                circuit_open = True
                                logger.warning(
                                    "Reddit: %d consecutive failures — opening circuit "
                                    "breaker; remaining subreddits will be skipped this run",
                                    consecutive_fail,
                                )
                            continue
                        consecutive_fail = 0
                        items.extend(new_items)
                        sub_count += len(new_items)
                    logger.info("r/%s: collected %d items (sorts: %s%s)",
                                sub, sub_count, ", ".join(sort_orders),
                                " via RSS" if json_disabled else "")
                    # Checkpoint: a sub counts as done only if at least one
                    # sort succeeded; an all-fail sub stays retryable.
                    if progress and not circuit_open:
                        progress.mark(
                            sub,
                            "failed" if (sub_failed and sub_count == 0) else "done",
                            sub_count, now_iso,
                        )
        except Exception as e:
            logger.error("Reddit collector (public) failed: %s", e)

        logger.info("Reddit public total: %d items from %d subreddits",
                     len(items), len(subreddits))
        return items

    def _parse_json(self, data: dict, sub: str, since_ts: float) -> list[RawItem]:
        """Build RawItems from a Reddit listing JSON payload."""
        out: list[RawItem] = []
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
            out.append(RawItem(
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
        return out

    async def _fetch_rss(self, session: aiohttp.ClientSession, sub: str,
                         sort: str, since_ts: float) -> list[RawItem]:
        """Fetch + parse a subreddit RSS feed (the unblocked fallback path).

        RSS gives no score/comment counts, so engagement/discussion-heat
        score components fall back to 0 for these items — the AI quality
        filter and recency/code/author dimensions still apply.
        """
        t_param = "&t=week" if sort == "top" else ""
        url = f"https://www.reddit.com/r/{sub}/{sort}/.rss?limit=100{t_param}"
        # Reddit RSS throttles with 429 under load; give transient limits
        # more room to clear (honoring Retry-After) before giving up on a
        # sort. Slow-but-complete beats fast-but-dropped.
        text = await self._fetch_text(session, url, max_retries=self._rss_max_retries)
        text = _BAD_XML_CHARS.sub("", text)
        feed = feedparser.parse(text)

        out: list[RawItem] = []
        for e in feed.entries:
            pp = e.get("published_parsed") or e.get("updated_parsed")
            if not pp:
                continue
            published = datetime(*pp[:6], tzinfo=timezone.utc)
            if published.timestamp() < since_ts:
                continue

            title = e.get("title", "")
            html = ""
            if e.get("content"):
                html = e["content"][0].get("value", "")
            elif e.get("summary"):
                html = e.get("summary", "")
            # Strip tags to a plain-text body for keyword matching + storage
            content_text = re.sub(r"<[^>]+>", " ", html)
            content_text = re.sub(r"\s+", " ", content_text).strip()

            if sub not in self._ai_subs and not self._matches_keywords(f"{title} {content_text}"):
                continue

            author = (e.get("author", "") or "").strip().lstrip("/")
            if author.startswith("u/"):
                author = author[2:]
            out.append(RawItem(
                source="reddit",
                title=title,
                url=e.get("link", ""),
                author=author,
                published_at=published,
                content=content_text[:500],
                metadata={
                    "subreddit": sub,
                    "sort": sort,
                    "reddit_id": e.get("id", ""),
                    "via": "rss",
                },
            ))
        return out

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
