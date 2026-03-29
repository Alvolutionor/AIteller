# src/utils/extractor.py
import asyncio
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

import aiohttp
import trafilatura

logger = logging.getLogger(__name__)


class ContentExtractor:
    def __init__(self, db=None, timeout: int = 10, max_size: int = 5 * 1024 * 1024):
        self.db = db
        self.timeout = timeout
        self.max_size = max_size
        self._domain_last_request: dict[str, float] = defaultdict(float)
        self._domain_delay = 2.0  # seconds between requests to same domain
        self._domain_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "AIteller/1.0 (news aggregator)"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _rate_limit(self, domain: str):
        async with self._domain_locks[domain]:
            now = time.monotonic()
            elapsed = now - self._domain_last_request[domain]
            if elapsed < self._domain_delay:
                await asyncio.sleep(self._domain_delay - elapsed)
            self._domain_last_request[domain] = time.monotonic()

    async def extract(self, url: str) -> str | None:
        """Extract article text from URL. Returns None on failure."""
        # Check cache
        if self.db:
            cached = await self.db.get_extracted_content(url)
            if cached is not None:
                return cached if cached else None

        # Rate limit per domain
        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        try:
            session = await self._get_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Extractor: {url} returned {resp.status}")
                    return None
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    logger.debug(f"Extractor: skipping non-HTML {url}")
                    return None
                if resp.content_length and resp.content_length > self.max_size:
                    logger.debug(f"Extractor: skipping oversized {url}")
                    return None
                html = await resp.text()

            text = trafilatura.extract(html, include_comments=False, include_tables=True)

            # Cache result
            if self.db:
                await self.db.save_extracted_content(url, text or "")

            return text
        except Exception as e:
            logger.warning(f"Extractor failed for {url}: {e}")
            return None
