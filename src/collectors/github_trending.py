# src/collectors/github_trending.py
"""GitHub collector: Trending page + Search API + Release tracking.

Three strategies:
  1. Trending page scraping (daily/weekly, per-language) — catches viral repos
  2. Search API (keyword + topic + pushed:>date) — catches quality AI content
  3. Release atom feeds are handled by rss_blogs collector via feeds.yaml
"""
import logging
import re
from datetime import datetime, timezone

import aiohttp
from bs4 import BeautifulSoup

from .base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

GITHUB_TRENDING_URL = "https://github.com/trending"
GITHUB_SEARCH_API = "https://api.github.com/search/repositories"

# Fallbacks if config has no github_topics/github_keywords
_DEFAULT_TOPICS = ["llm", "ai-agent", "rag"]
_DEFAULT_KEYWORDS = ["AI agent framework", "LLM inference"]


class GitHubTrendingCollector(BaseCollector):
    """Collect GitHub repos from trending page + search API."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rate_limit_delay = 3.0
        gh_cfg = config.get("sources", {}).get("github_trending", {})
        self.languages = gh_cfg.get("languages", ["python", "typescript", "rust"])
        self.github_token = gh_cfg.get("token", "")
        self.min_stars = gh_cfg.get("min_stars", 5)
        kw_cfg = config.get("keywords", {})
        self._search_topics: list[str] = kw_cfg.get("github_topics", []) or _DEFAULT_TOPICS
        self._search_keywords: list[str] = kw_cfg.get("github_keywords", []) or _DEFAULT_KEYWORDS

    def _api_headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AIteller/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def collect(self, since: datetime) -> list[RawItem]:
        items: list[RawItem] = []

        async with aiohttp.ClientSession() as session:
            # Strategy 1: Trending page
            trending = await self._collect_trending(session)
            items.extend(trending)

            # Strategy 2: Search API
            search_results = await self._collect_search(session, since)
            items.extend(search_results)

        # Dedup by URL
        seen: set[str] = set()
        unique: list[RawItem] = []
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        logger.info("GitHub total: %d unique repos (trending=%d, search=%d)",
                     len(unique), len(trending), len(search_results))
        return unique

    # ------------------------------------------------------------------
    # Strategy 1: Trending page
    # ------------------------------------------------------------------

    async def _collect_trending(self, session: aiohttp.ClientSession) -> list[RawItem]:
        items = []
        combos = [("", "daily"), ("", "weekly")]
        for lang in self.languages:
            combos.append((lang, "daily"))

        for lang, period in combos:
            try:
                lang_path = f"/{lang}" if lang else ""
                url = f"{GITHUB_TRENDING_URL}{lang_path}?since={period}"
                html = await self._fetch_text(session, url)
                parsed = self._parse_trending(html, lang or "all")
                items.extend(parsed)
            except Exception as e:
                logger.warning("GitHub Trending failed for %s/%s: %s (continuing)",
                               lang or "all", period, e)
                continue
        return items

    def _parse_trending(self, html: str, language: str) -> list[RawItem]:
        soup = BeautifulSoup(html, "html.parser")
        items = []

        for article in soup.select("article.Box-row"):
            try:
                h2 = article.select_one("h2 a")
                if not h2:
                    continue
                repo_path = h2.get("href", "").strip("/")
                if not repo_path:
                    continue
                repo_name = repo_path.split("/")[-1]
                owner = repo_path.split("/")[0] if "/" in repo_path else ""

                desc_elem = article.select_one("p")
                description = desc_elem.get_text(strip=True) if desc_elem else ""

                star_links = article.select("a.Link--muted")
                total_stars = 0
                if star_links:
                    star_text = star_links[0].get_text(strip=True).replace(",", "")
                    try:
                        total_stars = int(star_text)
                    except ValueError:
                        pass

                today_stars = 0
                today_span = article.select_one("span.d-inline-block.float-sm-right")
                if today_span:
                    match = re.search(r"([\d,]+)", today_span.get_text())
                    if match:
                        today_stars = int(match.group(1).replace(",", ""))

                items.append(RawItem(
                    source="github_trending",
                    title=f"{owner}/{repo_name}" if owner else repo_name,
                    url=f"https://github.com/{repo_path}",
                    author=owner,
                    published_at=datetime.now(timezone.utc),
                    content=description,
                    metadata={
                        "stars": total_stars,
                        "today_stars": today_stars,
                        "language": language,
                        "strategy": "trending",
                    },
                ))
            except Exception as e:
                logger.debug("Failed to parse trending repo: %s", e)
        return items

    # ------------------------------------------------------------------
    # Strategy 2: Search API
    # ------------------------------------------------------------------

    async def _collect_search(self, session: aiohttp.ClientSession,
                              since: datetime) -> list[RawItem]:
        """Search GitHub for recently active AI repos."""
        items: list[RawItem] = []
        headers = self._api_headers()
        pushed_since = since.strftime("%Y-%m-%d")
        consecutive_403 = 0

        # Topic-based searches
        for topic in self._search_topics:
            if consecutive_403 >= 3:
                logger.warning("GitHub Search: 3 consecutive 403s, budget exhausted — stopping searches")
                break
            query = f"topic:{topic} pushed:>{pushed_since} stars:>={self.min_stars}"
            batch, was_403 = await self._search_repos(session, headers, query)
            if was_403:
                consecutive_403 += 1
            else:
                consecutive_403 = 0
            items.extend(batch)

        # Keyword-based searches
        for keyword in self._search_keywords:
            if consecutive_403 >= 3:
                logger.warning("GitHub Search: 3 consecutive 403s, budget exhausted — stopping searches")
                break
            query = f"{keyword} in:name,description pushed:>{pushed_since} stars:>={self.min_stars}"
            batch, was_403 = await self._search_repos(session, headers, query)
            if was_403:
                consecutive_403 += 1
            else:
                consecutive_403 = 0
            items.extend(batch)

        return items

    async def _search_repos(self, session: aiohttp.ClientSession,
                            headers: dict, query: str) -> tuple[list[RawItem], bool]:
        """Execute a single search query, return up to 30 repos and a flag indicating 403."""
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": 30,
        }

        try:
            await self._rate_limit()
            async with session.get(GITHUB_SEARCH_API, headers=headers,
                                   params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 403:
                    # Rate limited — check header
                    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                    logger.warning("GitHub Search rate limited (remaining=%s), stopping", remaining)
                    return [], True
                if resp.status == 422:
                    logger.debug("GitHub Search 422 for query: %s", query)
                    return [], False
                if resp.status != 200:
                    logger.warning("GitHub Search HTTP %s for query: %s", resp.status, query)
                    return [], False
                data = await resp.json()
        except Exception as e:
            logger.error("GitHub Search failed for '%s': %s", query, e)
            return [], False

        items: list[RawItem] = []
        for repo in data.get("items", []):
            try:
                # Skip archived repositories
                if repo.get("archived", False):
                    logger.debug("Skipping archived repo: %s", repo.get("full_name", ""))
                    continue

                full_name = repo.get("full_name", "")
                description = repo.get("description", "") or ""
                stars = repo.get("stargazers_count", 0)
                forks = repo.get("forks_count", 0)
                language = repo.get("language", "")
                topics = repo.get("topics", [])
                pushed_at_str = repo.get("pushed_at", "")
                created_at_str = repo.get("created_at", "")

                # Parse pushed_at for the timestamp
                if pushed_at_str:
                    pushed_at = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
                else:
                    pushed_at = datetime.now(timezone.utc)

                owner = full_name.split("/")[0] if "/" in full_name else ""

                # Extract license info
                license_info = repo.get("license")
                license_spdx = license_info.get("spdx_id", "") if isinstance(license_info, dict) else ""

                items.append(RawItem(
                    source="github_trending",
                    title=full_name,
                    url=repo.get("html_url", f"https://github.com/{full_name}"),
                    author=owner,
                    published_at=pushed_at,
                    content=description,
                    metadata={
                        "stars": stars,
                        "forks": forks,
                        "language": language or "",
                        "topics": topics[:10],
                        "open_issues": repo.get("open_issues_count", 0),
                        "created_at": created_at_str,
                        "strategy": "search_api",
                        "license": license_spdx,
                        "has_wiki": repo.get("has_wiki", False),
                        "homepage": repo.get("homepage", "") or "",
                        "watchers_count": repo.get("watchers_count", 0),
                        "default_branch": repo.get("default_branch", ""),
                        "updated_at": repo.get("updated_at", ""),
                    },
                ))
            except Exception as e:
                logger.debug("Failed to parse search result: %s", e)

        if items:
            logger.debug("GitHub Search '%s': %d repos", query[:50], len(items))
        return items, False
