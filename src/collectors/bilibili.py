# src/collectors/bilibili.py
"""Bilibili collector built on the Nemo2011/bilibili-api library.

The previous hand-rolled WBI implementation kept tripping Bilibili's risk
control (HTTP 412 / API code -412), which throttled collection down to a
fraction of the available videos. The library handles WBI signing, and its
``enable_auto_buvid`` + ``enable_bili_ticket`` settings fetch the cookies /
web ticket that defeat the -412 风控 wall on the anonymous search endpoint —
no login required. An optional Credential (from env) is still wired for the
subtitle helper, which Bilibili now gates behind a logged-in session.
"""
import asyncio
import logging
import os
import random
from datetime import datetime, timezone

import aiohttp
import bilibili_api
from bilibili_api import Credential, search, video

try:
    from bilibili_api.exceptions import ResponseCodeException
except Exception:  # pragma: no cover - defensive against lib layout changes
    class ResponseCodeException(Exception):  # type: ignore
        code = None

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_client_ready = False


def _ensure_client() -> None:
    """Select the aiohttp HTTP backend and enable anti-风控 mitigations once.

    Safe to call before the event loop creates the client: select_client and
    request_settings.* don't touch the loop; the client is built lazily on the
    first API call.
    """
    global _client_ready
    if _client_ready:
        return
    bilibili_api.select_client("aiohttp")
    # auto_buvid + bili_ticket are the two settings that fetch the buvid cookies
    # and web ticket Bilibili requires before it stops returning -412 on the
    # anonymous WBI search endpoint.
    bilibili_api.request_settings.set_enable_auto_buvid(True)
    try:
        bilibili_api.request_settings.set_enable_bili_ticket(True)
    except Exception:  # older lib without the setter
        pass
    try:
        bilibili_api.request_settings.set_timeout(30.0)
    except Exception:
        pass
    try:
        bilibili_api.request_settings.set_wbi_retry_times(3)
    except Exception:
        pass
    _client_ready = True


def _build_credential() -> Credential | None:
    """Build an optional Credential from env vars (used by the subtitle helper).

    Search does not need login; these only matter for AI-subtitle extraction,
    which Bilibili now serves only to authenticated sessions.
    """
    sessdata = os.environ.get("BILIBILI_SESSDATA")
    bili_jct = os.environ.get("BILIBILI_BILI_JCT")
    buvid3 = os.environ.get("BILIBILI_BUVID3")
    if not (sessdata or buvid3):
        return None
    try:
        return Credential(sessdata=sessdata, bili_jct=bili_jct, buvid3=buvid3)
    except Exception as exc:  # pragma: no cover
        logger.warning("BilibiliCollector: bad Credential env vars: %s", exc)
        return None


def _exc_code(exc: Exception) -> int | None:
    """Best-effort extraction of the Bilibili API error code from an exception."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    msg = str(exc)
    if "-412" in msg or "412" in msg:
        return -412
    return None


class BilibiliCollector(BaseCollector):
    """Collect Bilibili videos via bilibili-api's WBI-signed search."""

    # search.search_by_type caps the page at 42 regardless of the requested size
    _MAX_PAGE_SIZE = 42

    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 3.0  # min seconds between search requests
        bili_cfg = config.get("sources", {}).get("bilibili", {})
        self.max_pages = bili_cfg.get("max_pages", 10)
        self.page_size = min(int(bili_cfg.get("page_size", 42)), self._MAX_PAGE_SIZE)
        self._credential = _build_credential()

    async def collect(self, since: datetime) -> list[RawItem]:
        keywords: list[str] = self.config.get("keywords", {}).get("primary", [])
        if not keywords:
            logger.warning("BilibiliCollector: no primary keywords configured")
            return []

        _ensure_client()
        since_ts = since.timestamp()
        items: list[RawItem] = []

        try:
            for kw in keywords:
                kw_items = await self._collect_keyword(kw, since_ts)
                items.extend(kw_items)
                # Jittered pause between keywords to stay under the风控 radar.
                await asyncio.sleep(random.uniform(0.5, 2.0))
        finally:
            await _close_client()

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[RawItem] = []
        for item in items:
            if item.url and item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        logger.info("BilibiliCollector: %d unique items from %d keywords",
                    len(unique), len(keywords))
        return unique

    async def _collect_keyword(self, kw: str, since_ts: float) -> list[RawItem]:
        out: list[RawItem] = []
        for page in range(1, self.max_pages + 1):
            await self._rate_limit()
            try:
                data = await search.search_by_type(
                    keyword=kw,
                    search_type=search.SearchObjectType.VIDEO,
                    order_type=search.OrderVideo.PUBDATE,  # newest first
                    page=page,
                    page_size=self.page_size,
                )
            except ResponseCodeException as exc:
                code = _exc_code(exc)
                if code == -412:
                    logger.warning(
                        "BilibiliCollector: -412 风控 on '%s' page %d — backing off",
                        kw, page,
                    )
                    await asyncio.sleep(random.uniform(8, 15))
                else:
                    logger.error(
                        "BilibiliCollector: API code %s for '%s' page %d: %s",
                        code, kw, page, exc,
                    )
                break
            except Exception as exc:
                if _exc_code(exc) == -412:
                    logger.warning(
                        "BilibiliCollector: 412 风控 on '%s' page %d — backing off",
                        kw, page,
                    )
                    await asyncio.sleep(random.uniform(8, 15))
                else:
                    logger.error(
                        "BilibiliCollector: search failed for '%s' page %d: %s",
                        kw, page, exc,
                    )
                break

            results = (data or {}).get("result") or []
            if not results:
                break

            found_new = False
            for vid in results:
                pubdate = vid.get("pubdate", 0) or 0
                if pubdate <= since_ts:
                    continue
                found_new = True
                title = vid.get("title", "") or ""
                title = title.replace('<em class="keyword">', "").replace("</em>", "")
                desc = vid.get("description") or vid.get("desc") or ""
                if not self._matches_keywords(title + " " + desc):
                    continue

                bvid = vid.get("bvid") or ""
                if bvid:
                    url = f"https://www.bilibili.com/video/{bvid}"
                else:
                    url = vid.get("arcurl") or vid.get("url") or ""
                if not url:
                    continue

                out.append(
                    RawItem(
                        source="bilibili",
                        title=title,
                        url=url,
                        author=vid.get("author", "") or "",
                        published_at=datetime.fromtimestamp(pubdate, tz=timezone.utc),
                        content=desc or title,
                        metadata={
                            "views": vid.get("play"),
                            "likes": vid.get("like"),
                            "coin": vid.get("coin"),
                            "favorite": vid.get("favorites"),
                            "danmaku": vid.get("danmaku") or vid.get("video_review"),
                            "review": vid.get("review"),
                            "tag": vid.get("tag", ""),
                            "description": desc,
                            "up_name": vid.get("author", ""),
                            "duration": vid.get("duration", ""),
                            "bvid": bvid,
                        },
                    )
                )

            # PUBDATE order is newest-first: once a whole page is older than
            # `since`, every later page is too — stop paging this keyword.
            if not found_new:
                break
            num_pages = (data or {}).get("numPages") or 0
            if num_pages and page >= num_pages:
                break
        return out


async def _close_client() -> None:
    """Best-effort close of the library's shared aiohttp session.

    Suppresses 'Unclosed client session' warnings at process exit. Each CLI
    command is a fresh process running collect once, so closing here is safe.
    """
    try:
        client = bilibili_api.get_client()
        close = getattr(client, "close", None)
        if close is not None:
            await close()
    except Exception:
        pass


async def get_bilibili_subtitles(video_id: str) -> str | None:
    """Extract AI-generated subtitles from a Bilibili video via bilibili-api.

    video_id: BV id (e.g. 'BV1xx411c7mD'), av id (e.g. '116256711644513'),
              or a full bilibili.com/video/... URL.
    Returns plain subtitle text, or None if unavailable.

    Note: Bilibili now serves subtitle lists only to logged-in sessions; set
    BILIBILI_SESSDATA (+ BILIBILI_BILI_JCT / BILIBILI_BUVID3) in config/.env.
    """
    import re

    aid = None
    bvid = None
    if "bilibili.com" in video_id:
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

    _ensure_client()
    credential = _build_credential()
    try:
        v = video.Video(bvid=bvid, aid=aid, credential=credential)
        cid = await v.get_cid(0)
        sub_info = await v.get_subtitle(cid)
        subtitles = (sub_info or {}).get("subtitles", []) or []
        if not subtitles:
            logger.debug("get_bilibili_subtitles: no subtitles for %s "
                         "(login may be required)", video_id)
            return None

        sub_url = None
        for sub in subtitles:
            lang = sub.get("lan", "") or ""
            if "zh" in lang or "ai" in lang.lower():
                sub_url = sub.get("subtitle_url", "")
                break
        if not sub_url:
            sub_url = subtitles[0].get("subtitle_url", "")
        if not sub_url:
            return None
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url

        headers = {"User-Agent": _USER_AGENT, "Referer": "https://www.bilibili.com"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(sub_url) as resp:
                if resp.status != 200:
                    return None
                sub_data = await resp.json(content_type=None)
        body = sub_data.get("body", []) or []
        lines = [seg.get("content", "") for seg in body if seg.get("content")]
        return " ".join(lines) if lines else None
    except Exception as exc:
        logger.error("get_bilibili_subtitles(%s): %s", video_id, exc)
        return None
    finally:
        await _close_client()
