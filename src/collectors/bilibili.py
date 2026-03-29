# src/collectors/bilibili.py
import asyncio
import hashlib
import logging
import random
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

import aiohttp

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

BILIBILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
BILIBILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# WBI mixin key encoding table
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 34, 6, 11, 56, 20, 34, 36, 22, 54, 59, 20, 44, 52, 57,
    21, 63, 62,
]


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """Generate the mixin key from img_key and sub_key using the encoding table."""
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def _sign_params(params: dict, mixin_key: str) -> dict:
    """Sign request params with WBI. Adds wts and w_rid."""
    params = dict(params)
    params["wts"] = int(time.time())
    # Sort by key
    params = dict(sorted(params.items()))
    # Filter unsafe chars from values
    sanitized = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(sanitized)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


class BilibiliCollector(BaseCollector):
    """Collect Bilibili videos via the public search API with WBI signature."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 4.0
        self._mixin_key: str | None = None
        bili_cfg = config.get("sources", {}).get("bilibili", {})
        self.max_pages = bili_cfg.get("max_pages", 3)
        self.page_size = bili_cfg.get("page_size", 50)

    async def _ensure_wbi_key(self, session: aiohttp.ClientSession) -> str:
        """Fetch WBI keys from nav API and compute mixin key."""
        if self._mixin_key:
            return self._mixin_key
        try:
            data = await self._fetch_json(session, BILIBILI_NAV_API)
            wbi_img = data.get("data", {}).get("wbi_img", {})
            img_url: str = wbi_img.get("img_url", "")
            sub_url: str = wbi_img.get("sub_url", "")
            # Extract key from URL: last path segment without extension
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
            self._mixin_key = _get_mixin_key(img_key, sub_key)
            logger.info("BilibiliCollector: WBI mixin key obtained")
            return self._mixin_key
        except Exception as exc:
            logger.error("BilibiliCollector: failed to get WBI key: %s", exc)
            return ""

    async def collect(self, since: datetime) -> list[RawItem]:
        keywords: list[str] = self.config.get("keywords", {}).get("primary", [])
        if not keywords:
            logger.warning("BilibiliCollector: no primary keywords configured")
            return []

        since_ts = since.timestamp()
        items: list[RawItem] = []
        buvid3 = str(uuid.uuid4()) + "infoc"
        b_nut = str(int(time.time()))
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
            "Cookie": f"buvid3={buvid3}; b_nut={b_nut}",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            mixin_key = await self._ensure_wbi_key(session)

            for kw in keywords:
                page = 0
                while True:
                    page += 1
                    if self.max_pages > 0 and page > self.max_pages:
                        break
                    params = {
                        "search_type": "video",
                        "keyword": kw,
                        "order": "pubdate",
                        "page": page,
                        "page_size": self.page_size,
                    }
                    if mixin_key:
                        params = _sign_params(params, mixin_key)

                    # Add random jitter between page requests to look less automated
                    await asyncio.sleep(random.uniform(0, 2))

                    try:
                        data = await self._fetch_json(
                            session, BILIBILI_SEARCH_API, params=params
                        )
                    except Exception as exc:
                        logger.error("BilibiliCollector: API failed for '%s' page %d: %s", kw, page, exc)
                        break

                    code = data.get("code", -1)
                    if code != 0:
                        if code == -412:
                            logger.warning(
                                "BilibiliCollector: anti-crawl code %s for '%s', skipping keyword", code, kw
                            )
                        else:
                            logger.error(
                                "BilibiliCollector: API error code %s for '%s' page %d", code, kw, page
                            )
                        break

                    results = data.get("data", {}).get("result", []) or []
                    if not results:
                        break  # no more pages

                    hit_old = False
                    for video in results:
                        pubdate = video.get("pubdate", 0)
                        if pubdate <= since_ts:
                            hit_old = True
                            continue
                        published_at = datetime.fromtimestamp(pubdate, tz=timezone.utc)
                        title: str = video.get("title", "")
                        title = title.replace("<em class=\"keyword\">", "").replace("</em>", "")
                        desc: str = video.get("description", "")
                        if not self._matches_keywords(title + " " + desc):
                            continue
                        items.append(
                            RawItem(
                                source="bilibili",
                                title=title,
                                url=video.get("arcurl", ""),
                                author=video.get("author", ""),
                                published_at=published_at,
                                content=desc or title,
                                metadata={
                                    "views": video.get("play"),
                                    "likes": video.get("like"),
                                    "coin": video.get("coin"),
                                    "favorite": video.get("favorites"),
                                    "danmaku": video.get("danmaku"),
                                    "review": video.get("review"),
                                    "tag": video.get("tag", ""),
                                    "description": video.get("description", ""),
                                    "up_name": video.get("author", ""),
                                    "duration": video.get("duration", ""),
                                },
                            )
                        )
                    # If all results on this page are older than since, stop paging
                    if hit_old and not any(
                        v.get("pubdate", 0) > since_ts for v in results
                    ):
                        break

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[RawItem] = []
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        return unique


async def get_bilibili_subtitles(video_id: str) -> str | None:
    """Extract AI-generated subtitles from a Bilibili video.

    video_id: BV id (e.g., 'BV1xx411c7mD') or av id (e.g., '116256711644513')
              or full URL (e.g., 'http://www.bilibili.com/video/av116256711644513').
    Returns plain text of subtitles, or None if unavailable.
    """
    # Parse video_id from URL or raw id
    aid = None
    bvid = None
    if "bilibili.com" in video_id:
        # Extract from URL
        import re
        av_match = re.search(r"/video/av(\d+)", video_id)
        bv_match = re.search(r"/video/(BV\w+)", video_id)
        if av_match:
            aid = int(av_match.group(1))
        elif bv_match:
            bvid = bv_match.group(1)
    elif video_id.startswith("BV"):
        bvid = video_id
    elif video_id.isdigit():
        aid = int(video_id)
    else:
        bvid = video_id

    buvid3 = str(uuid.uuid4()) + "infoc"
    b_nut = str(int(time.time()))
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.bilibili.com",
        "Origin": "https://www.bilibili.com",
        "Cookie": f"buvid3={buvid3}; b_nut={b_nut}",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # Step 1: Get cid from video info API
            info_url = "https://api.bilibili.com/x/web-interface/view"
            params = {"aid": aid} if aid else {"bvid": bvid}
            async with session.get(info_url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get("code") != 0:
                    logger.debug("get_bilibili_subtitles: view API code=%s for %s", data.get("code"), video_id)
                    return None
                cid = data.get("data", {}).get("cid")
                aid = data.get("data", {}).get("aid")
                if not cid:
                    return None

            # Step 2: Get subtitle list from player API
            player_url = "https://api.bilibili.com/x/player/v2"
            params = {"aid": aid, "cid": cid}
            async with session.get(player_url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
                if not subtitles:
                    return None

            # Pick Chinese subtitle (ai_subtitle or manual)
            sub_url = None
            for sub in subtitles:
                lang = sub.get("lan", "")
                if "zh" in lang or "ai" in lang.lower():
                    sub_url = sub.get("subtitle_url", "")
                    break
            if not sub_url and subtitles:
                sub_url = subtitles[0].get("subtitle_url", "")
            if not sub_url:
                return None

            # Ensure https
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url

            # Step 3: Fetch subtitle content
            async with session.get(sub_url) as resp:
                if resp.status != 200:
                    return None
                sub_data = await resp.json()
                body = sub_data.get("body", [])
                if not body:
                    return None
                lines = [item.get("content", "") for item in body if item.get("content")]
                return " ".join(lines) if lines else None

    except Exception as exc:
        logger.error("get_bilibili_subtitles(%s): %s", bvid, exc)
        return None
