# src/collectors/youtube.py
"""YouTube collector using yt-dlp for keyword-based search (no API key needed).

Two-phase approach:
  Phase 1: Flat search with YouTube date filter → fast batch of video IDs + titles
  Phase 2: Full extraction for filtered videos → upload_date, subtitles, metadata
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from functools import partial

import yt_dlp

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

# YouTube search date filters (sp parameter)
_SP_TODAY = "EgIIAg%3D%3D"       # last 24h
_SP_THIS_WEEK = "EgIIAw%3D%3D"   # this week
_SP_THIS_MONTH = "EgIIBA%3D%3D"  # this month

_YDL_FLAT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "ignoreerrors": True,
}

_YDL_FULL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "ignoreerrors": True,
    "writesubtitles": False,
    "writeautomaticsub": False,
}


def _choose_date_filter(lookback_hours: int) -> str:
    """Pick a YouTube search date filter that covers the lookback period."""
    if lookback_hours <= 24:
        return _SP_TODAY
    elif lookback_hours <= 168:
        return _SP_THIS_WEEK
    return _SP_THIS_MONTH


class YouTubeCollector(BaseCollector):
    """Collect YouTube videos via keyword search using yt-dlp."""

    def __init__(self, config: dict, feeds: dict | None = None):
        super().__init__(config)
        self.rate_limit_delay = 2.0
        self.feeds: dict = feeds or {}
        yt_cfg = config.get("sources", {}).get("youtube", {})
        self.max_results_per_keyword = yt_cfg.get("max_results_per_keyword", 20)
        self.max_total_videos = yt_cfg.get("max_total_videos", 100)

    async def collect(self, since: datetime) -> list[RawItem]:
        keywords: list[str] = self.config.get("keywords", {}).get("primary", [])
        if not keywords:
            logger.warning("YouTubeCollector: no primary keywords configured")
            return []

        lookback_hours = self.config.get("collection", {}).get("lookback_hours", 24)
        sp_filter = _choose_date_filter(lookback_hours)

        # Phase 1: flat search for each keyword
        flat_results: list[dict] = []
        seen_ids: set[str] = set()

        for idx, kw in enumerate(keywords):
            batch = await self._flat_search(kw, sp_filter)
            for entry in batch:
                vid_id = entry.get("id", "")
                if vid_id and vid_id not in seen_ids:
                    seen_ids.add(vid_id)
                    flat_results.append(entry)
            # Rate limit between keyword searches
            if idx < len(keywords) - 1:
                await asyncio.sleep(self.rate_limit_delay)

        logger.info("YouTubeCollector: flat search returned %d unique videos", len(flat_results))

        if not flat_results:
            return []

        # Cap total videos if configured (0 = unlimited)
        if self.max_total_videos > 0 and len(flat_results) > self.max_total_videos:
            logger.info("YouTubeCollector: capping %d videos to max_total_videos=%d",
                        len(flat_results), self.max_total_videos)
            flat_results = flat_results[:self.max_total_videos]

        # Phase 2: full extraction for metadata + date filtering
        since_str = since.strftime("%Y%m%d")
        items = await self._extract_full(flat_results, since_str, since)

        logger.info("YouTubeCollector: %d videos after date filtering", len(items))
        return items

    async def _flat_search(self, keyword: str, sp_filter: str) -> list[dict]:
        """Run a flat YouTube search for one keyword."""
        import urllib.parse
        encoded_kw = urllib.parse.quote_plus(keyword)
        url = f"https://www.youtube.com/results?search_query={encoded_kw}&sp={sp_filter}"

        opts = {**_YDL_FLAT_OPTS, "playlistend": self.max_results_per_keyword}

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                partial(self._ydl_extract, opts, url),
            )
            entries = (result or {}).get("entries", [])
            logger.info("YouTubeCollector: '%s' → %d flat results", keyword, len(entries))
            return entries
        except Exception as exc:
            logger.error("YouTubeCollector: flat search failed for '%s': %s", keyword, exc)
            return []

    async def _extract_full(
        self,
        flat_entries: list[dict],
        since_str: str,
        since_dt: datetime,
    ) -> list[RawItem]:
        """Extract full metadata for videos, filtering by date."""
        video_ids = [e["id"] for e in flat_entries if e.get("id")]
        if not video_ids:
            return []

        # Build quick lookup for flat data
        flat_map = {e["id"]: e for e in flat_entries if e.get("id")}

        # Extract in batches to avoid overwhelming YouTube
        BATCH = 10
        items: list[RawItem] = []
        loop = asyncio.get_event_loop()

        for i in range(0, len(video_ids), BATCH):
            batch_ids = video_ids[i:i + BATCH]
            urls = [f"https://www.youtube.com/watch?v={vid}" for vid in batch_ids]

            opts = {
                **_YDL_FULL_OPTS,
                "dateafter": since_str,
            }

            for url in urls:
                vid_id = url.split("v=")[1]
                try:
                    info = await loop.run_in_executor(
                        None,
                        partial(self._ydl_extract, opts, url),
                    )
                    if not info:
                        continue

                    upload_date = info.get("upload_date", "")
                    if not upload_date or upload_date < since_str:
                        continue

                    # Parse upload_date YYYYMMDD
                    try:
                        pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue

                    title = info.get("title", "")
                    desc = info.get("description", "") or ""

                    # Keyword match
                    if not self._matches_keywords(title + " " + desc):
                        continue

                    # Collect subtitle language info
                    sub_langs = list((info.get("subtitles") or {}).keys())
                    auto_langs = list((info.get("automatic_captions") or {}).keys())

                    items.append(
                        RawItem(
                            source="youtube",
                            title=title,
                            url=info.get("webpage_url", url),
                            author=info.get("channel", info.get("uploader", "")),
                            published_at=pub_dt,
                            content=desc[:500],
                            metadata={
                                "views": info.get("view_count"),
                                "likes": info.get("like_count"),
                                "duration": info.get("duration"),
                                "channel": info.get("channel", ""),
                                "channel_id": info.get("channel_id", ""),
                                "video_id": vid_id,
                                "subtitle_langs": sub_langs[:5],
                                "auto_caption_langs": auto_langs[:5],
                                "comment_count": info.get("comment_count"),
                            },
                        )
                    )
                except Exception as exc:
                    logger.warning("YouTubeCollector: failed to extract %s: %s", vid_id, exc)

            # Rate limit between batches
            if i + BATCH < len(video_ids):
                await asyncio.sleep(5)

        return items

    @staticmethod
    def _ydl_extract(opts: dict, url: str) -> dict | None:
        """Synchronous yt-dlp extraction (run in executor)."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)


async def get_subtitles(video_id: str, lang: str = "en") -> str | None:
    """Extract subtitles/captions for a YouTube video.

    Gets subtitle URLs from yt-dlp extract_info, then fetches with aiohttp.
    Tries manual subtitles first, then auto-generated captions.
    Returns plain text of the subtitles, or None if unavailable.
    """
    import json
    import aiohttp

    url = f"https://www.youtube.com/watch?v={video_id}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}-orig"],
        "subtitlesformat": "json3",
        "ignoreerrors": True,
    }

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(
            None,
            partial(YouTubeCollector._ydl_extract, opts, url),
        )
        if not info:
            return None
    except Exception as exc:
        logger.error("get_subtitles(%s): extract_info failed: %s", video_id, exc)
        return None

    # Collect all subtitle URLs to try, preferring manual > auto, exact lang > prefix
    sub_urls = []
    for sub_dict_key in ("subtitles", "automatic_captions"):
        sub_dict = info.get(sub_dict_key, {}) or {}
        for sub_lang, formats in sub_dict.items():
            if not sub_lang.startswith(lang):
                continue
            for fmt in formats:
                sub_url = fmt.get("url", "")
                ext = fmt.get("ext", "")
                if sub_url and ext == "json3":
                    sub_urls.append(sub_url)

    if not sub_urls:
        return None

    # Fetch with browser-like headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.youtube.com/watch?v={video_id}",
    }

    backoff_429 = 15  # initial backoff for 429 responses, doubles each time
    for sub_url in sub_urls[:3]:  # try up to 3 URLs
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(sub_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 429:
                        logger.warning("get_subtitles(%s): rate limited (429), backing off %ds", video_id, backoff_429)
                        await asyncio.sleep(backoff_429)
                        backoff_429 = min(backoff_429 * 2, 60)
                        continue
                    if resp.status != 200:
                        continue
                    data = await resp.text()

                    # Parse json3
                    parsed = json.loads(data)
                    events = parsed.get("events", [])
                    lines = []
                    for ev in events:
                        segs = ev.get("segs", [])
                        text = "".join(s.get("utf8", "") for s in segs).strip()
                        if text and text != "\n":
                            lines.append(text)
                    if lines:
                        return " ".join(lines)
        except Exception as exc:
            logger.warning("get_subtitles(%s): fetch failed: %s", video_id, exc)
            continue

    return None
