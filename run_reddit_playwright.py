"""Reddit collection via a real headless browser (Playwright + Chromium).

Why this exists: as of 2026 Reddit's edge ("network security") hard-blocks
the public ``.json`` endpoints at the IP level — desktop UA, randomized iOS
Safari UA, browser-grade TLS impersonation (curl_cffi) and even a real
Chromium hitting ``*.json`` all return HTTP 403 from this host. The one path
that is NOT blocked is the **old.reddit.com HTML listing** rendered in a real
browser: it returns 200 and every post's metadata — exact score, comment
count, author, timestamp, permalink — is embedded in the ``div.thing``
``data-*`` attributes. So we drive Chromium, scrape those attributes, and get
the same fidelity as the old ``.json`` path (real scores, not an RSS proxy).

Rows are written into the existing ``raw_items`` table with
``batch_id="reddit-pw-<run_id>"``; run ``python -m src.main rescore``
afterwards to filter + score them through the normal pipeline.

Usage:  uv run --with playwright python run_reddit_playwright.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from src.config import load_config  # noqa: E402
from src.storage.db import Database  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reddit_pw")

# Installed Chromium (Playwright cache). Override with CHROME_EXE env.
_DEFAULT_CHROME = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "ms-playwright" / "chromium-1217" / "chrome-win64" / "chrome.exe"
)
CHROME_EXE = os.environ.get("CHROME_EXE", str(_DEFAULT_CHROME))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Subreddits where everything is on-topic — skip the keyword gate.
AI_SUBS = {
    "LocalLLaMA", "LocalLLM", "MachineLearning", "ClaudeAI", "ChatGPT",
    "ChatGPTPro", "ChatGPTCoding", "artificial", "ArtificialIntelligence",
    "LangChain", "Rag", "ollama", "OpenAI", "Anthropic", "GoogleGeminiAI",
    "grok", "perplexity_ai", "deeplearning", "reinforcementlearning",
    "singularity", "AI_Agents", "MCPServers", "LLMDevs", "ClaudeCode",
    "CursorAI", "PromptEngineering", "vibecoding", "learnmachinelearning",
    "StableDiffusion", "midjourney", "comfyui",
}

# Extract every data-* attribute + the post title from the listing's
# non-promoted link rows.
_EXTRACT_JS = """
els => els.map(e => {
  const d = {};
  for (const a of e.attributes) if (a.name.startsWith('data-')) d[a.name] = a.value;
  const t = e.querySelector('a.title');
  d._title = t ? t.innerText : '';
  return d;
})
"""


def scrape_subreddit(page, sub: str, sort: str, max_pages: int,
                     since_ts: float) -> list[dict]:
    """Load an old.reddit listing for ``sort`` and paginate via the "next"
    button up to ``max_pages``, returning all post dicts (data-* attrs).

    Early-stops pagination when a full page of posts is already older than
    the collection window (only meaningful for recency-ordered sorts —
    ``new``/``rising``/``hot``; ``top`` is score-ordered so we just page to
    the cap)."""
    t_param = "?t=week" if sort == "top" else ""
    url = f"https://old.reddit.com/r/{sub}/{sort}/{t_param}"
    all_rows: list[dict] = []
    for page_idx in range(max_pages):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
        status = resp.status if resp else 0
        if status != 200:
            if page_idx == 0:
                body = ""
                try:
                    body = page.inner_text("body")[:120]
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {status} for {url}: {body}")
            break  # deeper page failed — keep what we have
        rows = page.eval_on_selector_all("div.thing.link:not(.promoted)", _EXTRACT_JS)
        if not rows:
            break
        all_rows.extend(rows)
        # Recency-ordered sorts: stop once the whole page predates the window
        if sort != "top":
            try:
                newest = max(int(r.get("data-timestamp") or 0) for r in rows) / 1000.0
                if newest < since_ts:
                    break
            except ValueError:
                pass
        # Find the "next" link for the following page
        nxt = page.query_selector("span.next-button a")
        if not nxt:
            break
        url = nxt.get_attribute("href")
        if not url:
            break
        time.sleep(random.uniform(1.5, 3.0))
    return all_rows


def to_row(d: dict, sub: str, sort: str, since_ts: float, min_score: int,
           primary_keywords: list[str]) -> dict | None:
    """Convert scraped data-* dict to a raw_items row, applying filters."""
    try:
        ts_ms = int(d.get("data-timestamp") or 0)
        created = ts_ms / 1000.0
        score = int(d.get("data-score") or 0)
        comments = int(d.get("data-comments-count") or 0)
    except ValueError:
        return None
    if created < since_ts or score < min_score:
        return None

    title = (d.get("_title") or "").strip()
    if not title:
        return None
    if sub not in AI_SUBS and primary_keywords:
        low = title.lower()
        if not any(kw.lower() in low for kw in primary_keywords):
            return None

    permalink = d.get("data-permalink") or ""
    url = f"https://reddit.com{permalink}" if permalink else (d.get("data-url") or "")
    link_url = d.get("data-url") or ""
    is_self = str(d.get("data-domain", "")).startswith("self.")

    return {
        "source": "reddit",
        "title": title,
        "url": url,
        "author": d.get("data-author") or "",
        "published_at": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
        "content": "",  # listing doesn't expose selftext; title carries the signal
        "metadata": json.dumps({
            "score": score,
            "num_comments": comments,
            "subreddit": sub,
            "sort": sort,
            "reddit_id": d.get("data-fullname", ""),
            "has_url": bool(link_url) and not is_self,
            "link_url": "" if is_self else link_url,
            "domain": d.get("data-domain", ""),
            "is_self": is_self,
            "gilded": int(d.get("data-gildings") or 0),
            "num_crossposts": int(d.get("data-num-crossposts") or 0),
            "rank": int(d.get("data-rank") or 0),
            "via": "playwright",
        }, ensure_ascii=False),
        "batch_id": None,  # filled in by caller
    }


def scrape_all(subs: list[str], sort_pages: dict[str, int], since_ts: float,
               min_score: int, primary_keywords: list[str]) -> list[dict]:
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    seen_urls: set[str] = set()
    ok = 0
    fail = 0
    consecutive_fail = 0
    sorts = list(sort_pages)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_EXE, headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()
        try:
            for i, sub in enumerate(subs):
                # Periodic long cooldown to avoid the per-IP rate limit that
                # trips after a few hundred rapid page loads.
                if i > 0 and i % 5 == 0:
                    cooldown = random.uniform(30, 50)
                    logger.info("Cooldown %.0fs after %d subs", cooldown, i)
                    time.sleep(cooldown)
                sub_rows = 0
                for sort in sorts:
                    # Polite pacing between subreddit/sort sweeps
                    if i > 0 or sort != sorts[0]:
                        time.sleep(random.uniform(3.0, 5.5))
                    try:
                        scraped = scrape_subreddit(page, sub, sort,
                                                   sort_pages[sort], since_ts)
                        consecutive_fail = 0
                    except Exception as e:
                        fail += 1
                        consecutive_fail += 1
                        logger.warning("✗ r/%s [%s]: %s", sub, sort, str(e)[:140])
                        if consecutive_fail >= 6:
                            logger.error("6 consecutive failures — stopping early")
                            return rows
                        continue
                    for d in scraped:
                        row = to_row(d, sub, sort, since_ts, min_score, primary_keywords)
                        if not row:
                            continue
                        if row["url"] in seen_urls:
                            continue
                        seen_urls.add(row["url"])
                        rows.append(row)
                        sub_rows += 1
                ok += 1
                logger.info("✓ r/%s: %d posts kept (running total: %d)", sub, sub_rows, len(rows))
        finally:
            browser.close()

    logger.info("Scrape done: %d subs ok, %d sub/sort failures, %d unique posts",
                ok, fail, len(rows))
    return rows


async def insert_all(db_path: str, rows: list[dict], batch_id: str) -> int:
    db = Database(db_path)
    await db.initialize()
    inserted = 0
    try:
        for row in rows:
            row = dict(row)
            row["batch_id"] = batch_id
            if await db.insert_raw_item(row):
                inserted += 1
    finally:
        await db.close()
    return inserted


def main():
    if not Path(CHROME_EXE).exists():
        logger.error("Chromium not found at %s — set CHROME_EXE", CHROME_EXE)
        sys.exit(1)

    cfg = load_config(str(BASE_DIR / "config" / "config.yaml"), strict=False)
    rcfg = cfg["sources"]["reddit"]
    subs = rcfg.get("subreddits", []) or []
    # Optional: restrict to a comma-separated subset (for completing a
    # partial run without re-scraping subs already covered).
    if os.environ.get("REDDIT_SUBS"):
        wanted = [s.strip() for s in os.environ["REDDIT_SUBS"].split(",") if s.strip()]
        subs = [s for s in subs if s in wanted] or wanted
    # "Get everything" mode: drop the high-score gate (env override) so the
    # AI filter — not an arbitrary upvote threshold — decides relevance.
    min_score = int(os.environ.get("REDDIT_MIN_SCORE", "1"))
    lookback = int(cfg["collection"]["lookback_hours"])
    primary_keywords = cfg.get("keywords", {}).get("primary", []) or []
    db_path = cfg["storage"]["db_path"]

    since = datetime.now(timezone.utc) - timedelta(hours=lookback)
    since_ts = since.timestamp()
    # Per-sort page caps (each page ≈ 25 posts). top=week's best (paged deep),
    # new/rising/hot=recency-ordered (early-stop once out of window).
    sort_pages = {
        "top": int(os.environ.get("REDDIT_TOP_PAGES", "4")),
        "hot": int(os.environ.get("REDDIT_HOT_PAGES", "2")),
        "new": int(os.environ.get("REDDIT_NEW_PAGES", "4")),
        "rising": int(os.environ.get("REDDIT_RISING_PAGES", "1")),
    }
    batch_id = f"reddit-pw-{uuid.uuid4().hex[:8]}"

    logger.info("Reddit (Playwright): %d subs, sort_pages=%s, lookback=%dh, "
                "min_score=%d, batch=%s", len(subs), sort_pages, lookback, min_score, batch_id)

    rows = scrape_all(subs, sort_pages, since_ts, min_score, primary_keywords)
    if not rows:
        logger.warning("No rows scraped — nothing to insert")
        return
    inserted = asyncio.run(insert_all(db_path, rows, batch_id))
    logger.info("DONE: scraped %d unique posts, inserted %d new, batch=%s",
                len(rows), inserted, batch_id)


if __name__ == "__main__":
    main()
