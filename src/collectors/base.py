# src/collectors/base.py
"""Base collector with built-in rate limiting, 429/503 backoff, and retry."""
import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

# Status codes that warrant automatic retry with backoff
_RETRY_STATUS = {429, 403, 503, 502, 500}


@dataclass
class RawItem:
    source: str
    title: str
    url: str
    author: str
    published_at: datetime
    content: str
    metadata: dict = field(default_factory=dict)


class BaseCollector(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.rate_limit_delay: float = 1.0  # min seconds between requests
        self._last_request_time: float = 0.0
        self._consecutive_errors: int = 0

    @abstractmethod
    async def collect(self, since: datetime) -> list[RawItem]:
        pass

    async def _rate_limit(self):
        """Enforce minimum delay between requests."""
        if self.rate_limit_delay <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_time
        wait = self.rate_limit_delay - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = time.monotonic()

    async def _backoff(self, attempt: int, url: str, status: int):
        """Exponential backoff with jitter on rate limit / server error."""
        base = min(2 ** attempt * 5, 120)  # 5s, 10s, 20s, 40s, ... up to 120s
        jitter = random.uniform(0, base * 0.3)
        wait = base + jitter
        logger.warning("%s: HTTP %d from %s — backing off %.0fs (attempt %d)",
                       self.__class__.__name__, status, url[:80], wait, attempt + 1)
        await asyncio.sleep(wait)

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str,
                          max_retries: int = 3, **kwargs) -> dict:
        """Fetch JSON with rate limiting, retry on 429/5xx, fail on 4xx."""
        for attempt in range(max_retries):
            await self._rate_limit()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30),
                                       **kwargs) as resp:
                    if resp.status == 200:
                        self._consecutive_errors = 0
                        return await resp.json()

                    if resp.status in _RETRY_STATUS:
                        self._consecutive_errors += 1
                        await self._backoff(attempt, url, resp.status)
                        continue

                    # Non-retryable error (404, 403, etc.)
                    text = await resp.text()
                    logger.warning("%s: HTTP %d from %s: %s",
                                   self.__class__.__name__, resp.status,
                                   url[:80], text[:200])
                    resp.raise_for_status()

            except asyncio.TimeoutError:
                self._consecutive_errors += 1
                if attempt < max_retries - 1:
                    await self._backoff(attempt, url, 0)
                else:
                    raise
            except aiohttp.ClientResponseError:
                raise  # already logged above
            except (aiohttp.ClientError, OSError) as e:
                self._consecutive_errors += 1
                if attempt < max_retries - 1:
                    logger.warning("%s: connection error %s — retrying",
                                   self.__class__.__name__, e)
                    await self._backoff(attempt, url, 0)
                else:
                    raise

        raise RuntimeError(f"Max retries ({max_retries}) exceeded for {url[:80]}")

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str,
                          max_retries: int = 3, **kwargs) -> str:
        """Fetch text with rate limiting, retry on 429/5xx, fail on 4xx."""
        for attempt in range(max_retries):
            await self._rate_limit()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30),
                                       **kwargs) as resp:
                    if resp.status == 200:
                        self._consecutive_errors = 0
                        return await resp.text()

                    if resp.status in _RETRY_STATUS:
                        self._consecutive_errors += 1
                        await self._backoff(attempt, url, resp.status)
                        continue

                    text = await resp.text()
                    logger.warning("%s: HTTP %d from %s: %s",
                                   self.__class__.__name__, resp.status,
                                   url[:80], text[:200])
                    resp.raise_for_status()

            except asyncio.TimeoutError:
                self._consecutive_errors += 1
                if attempt < max_retries - 1:
                    await self._backoff(attempt, url, 0)
                else:
                    raise
            except aiohttp.ClientResponseError:
                raise
            except (aiohttp.ClientError, OSError) as e:
                self._consecutive_errors += 1
                if attempt < max_retries - 1:
                    logger.warning("%s: connection error %s — retrying",
                                   self.__class__.__name__, e)
                    await self._backoff(attempt, url, 0)
                else:
                    raise

        raise RuntimeError(f"Max retries ({max_retries}) exceeded for {url[:80]}")

    def _matches_keywords(self, text: str) -> bool:
        """Check if text matches any primary keyword (case-insensitive)."""
        keywords = self.config.get("keywords", {}).get("primary", [])
        if not keywords:
            return True
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in keywords)
