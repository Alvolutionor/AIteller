"""Supplemental Reddit collection for top + rising sorts.

Run AFTER the main weekly `collect` finishes (which only fetches `hot`).
This script adds the `top` (t=week) and `rising` slices on top of what
the main pipeline already collected. Inserts directly into raw_items
under batch_id "reddit-suppl-<run_id>"; URL dedup at insert time
naturally drops anything that overlapped with hot.

Pacing matches the main RedditCollector: 8s rate_limit_delay, 8-15s
inter-sub jitter, long 30-60s pause every 5 subs, circuit breaker
opens on 3 consecutive sub failures. Two passes (top, rising) with
60-120s cooldown between them so Reddit's per-IP request counter
gets a breather.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from src.collectors.base import RawItem  # noqa: E402
from src.config import load_config  # noqa: E402
from src.storage.db import Database  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reddit_suppl")


HOST_CANDIDATES = ["www.reddit.com", "old.reddit.com"]


async def fetch_one(session, host, sub, sort, t_param):
    url = f"https://{host}/r/{sub}/{sort}.json?limit=100{t_param}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status == 200:
            return await r.json()
        # Read body to drain connection but discard
        await r.text()
        raise RuntimeError(f"HTTP {r.status} for {url}")


async def fetch_sub(session, sub, sort, since_ts, min_score, ai_subs, primary_keywords, good_host):
    """Fetch one (sub, sort) and return (items, host_used)."""
    t_param = "&t=week" if sort == "top" else ""
    hosts = [good_host] if good_host else HOST_CANDIDATES
    last_err = None
    for host in hosts:
        try:
            data = await fetch_one(session, host, sub, sort, t_param)
            items = []
            for post in data.get("data", {}).get("children", []):
                pd = post.get("data", {})
                created = pd.get("created_utc", 0)
                score = pd.get("score", 0)
                if created < since_ts or score < min_score:
                    continue
                text = f"{pd.get('title', '')} {pd.get('selftext', '')}".lower()
                if sub not in ai_subs and primary_keywords and not any(
                    kw.lower() in text for kw in primary_keywords
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
                        "sort": sort,
                    },
                ))
            return items, host
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("all hosts failed")


async def insert_items(db, items, batch_id):
    inserted = 0
    for item in items:
        row = {
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "author": item.author,
            "published_at": item.published_at.isoformat(),
            "content": item.content,
            "metadata": json.dumps(item.metadata, ensure_ascii=False),
            "batch_id": batch_id,
        }
        if await db.insert_raw_item(row):
            inserted += 1
    return inserted


async def run_pass(cfg, sort_name: str, batch_id: str, lookback_hours: int):
    """One full pass over all subs for a given sort."""
    rcfg = cfg["sources"]["reddit"]
    subs = rcfg.get("subreddits", [])
    min_score = rcfg.get("min_score", 15)
    rate = float(rcfg.get("rate_limit_delay", 8.0))
    inter_sub_min = float(rcfg.get("inter_sub_min", 8.0))
    inter_sub_max = float(rcfg.get("inter_sub_max", 15.0))
    long_every = int(rcfg.get("long_pause_every", 5))
    long_min = float(rcfg.get("long_pause_min", 30.0))
    long_max = float(rcfg.get("long_pause_max", 60.0))
    cb_threshold = int(rcfg.get("circuit_breaker_after", 3))

    primary_keywords = cfg.get("keywords", {}).get("primary", []) or []
    ai_subs = {
        "LocalLLaMA", "ClaudeAI", "ChatGPT", "ChatGPTCoding", "LangChain",
        "Rag", "ollama", "OpenAI", "Anthropic", "GoogleGeminiAI",
        "ArtificialIntelligence", "MLOps", "LocalLLM", "ChatGPTPro",
        "artificial", "MachineLearning", "deeplearning", "reinforcementlearning",
        "AI_Agents", "MCPServers", "LLMDevs", "ClaudeCode", "CursorAI",
        "PromptEngineering", "vibecoding", "learnmachinelearning",
        "StableDiffusion", "midjourney", "comfyui", "civitai", "aivideo",
        "grok", "perplexity_ai", "singularity",
    }

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    since_ts = since.timestamp()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/html;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }

    db = Database(cfg["storage"]["db_path"])
    await db.initialize()
    good_host = None
    consecutive_fail = 0
    circuit_open = False
    total_collected = 0
    total_inserted = 0
    success_count = 0

    logger.info("=== Pass: sort=%s, %d subs, min_score=%d ===",
                sort_name, len(subs), min_score)

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            last_request_at = 0.0
            for sub_idx, sub in enumerate(subs):
                if circuit_open:
                    logger.info("r/%s [%s]: skipped (circuit open)", sub, sort_name)
                    continue

                if sub_idx > 0:
                    if long_every > 0 and sub_idx % long_every == 0:
                        pause = random.uniform(long_min, long_max)
                        logger.info("Long pause %.0fs after %d subs", pause, sub_idx)
                        await asyncio.sleep(pause)
                    else:
                        await asyncio.sleep(random.uniform(inter_sub_min, inter_sub_max))

                # Rate limit between requests
                wait = rate - (time.monotonic() - last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                last_request_at = time.monotonic()

                try:
                    items, used_host = await fetch_sub(
                        session, sub, sort_name, since_ts, min_score,
                        ai_subs, primary_keywords, good_host,
                    )
                    good_host = used_host
                    consecutive_fail = 0
                    inserted = await insert_items(db, items, batch_id)
                    total_collected += len(items)
                    total_inserted += inserted
                    success_count += 1
                    logger.info("✓ r/%s [%s]: %d collected, %d new (running: %d / %d)",
                                sub, sort_name, len(items), inserted,
                                total_collected, total_inserted)
                except Exception as e:
                    consecutive_fail += 1
                    logger.warning("✗ r/%s [%s]: %s (consec fail: %d)",
                                   sub, sort_name, e, consecutive_fail)
                    if consecutive_fail >= cb_threshold:
                        circuit_open = True
                        logger.warning("CIRCUIT OPEN after %d consec fails — "
                                       "remaining subs skipped", consecutive_fail)
    finally:
        await db.close()

    logger.info("=== Pass done: sort=%s, %d subs ok, %d collected, %d inserted ===",
                sort_name, success_count, total_collected, total_inserted)
    return total_inserted


async def main():
    cfg = load_config(str(BASE_DIR / "config" / "config.yaml"), strict=False)
    batch_id = f"reddit-suppl-{uuid.uuid4().hex[:8]}"
    lookback = cfg["collection"]["lookback_hours"]

    logger.info("Reddit supplemental: batch=%s, lookback=%dh", batch_id, lookback)

    # Pass 1: top
    inserted_top = await run_pass(cfg, "top", batch_id, lookback)

    # Cooldown between passes — give Reddit's per-IP rate counter time to decay
    cooldown = random.uniform(60, 120)
    logger.info("Cooldown %.0fs between top and rising passes", cooldown)
    await asyncio.sleep(cooldown)

    # Pass 2: rising
    inserted_rising = await run_pass(cfg, "rising", batch_id, lookback)

    logger.info("Reddit supplemental DONE: top=%d new, rising=%d new, total=%d, batch=%s",
                inserted_top, inserted_rising, inserted_top + inserted_rising, batch_id)


if __name__ == "__main__":
    asyncio.run(main())
