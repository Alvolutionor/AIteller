"""Re-collect Reddit on its own, using the hardened RedditCollector.

This runs the same RSS-first collector the main pipeline uses, but in
isolation so we can refresh Reddit without re-hitting every other source.
It inherits all the 2026 anti-429 work:

  * conservative pacing (8-15s between subs, long pause every 5 subs,
    3-7s between sort orders),
  * Retry-After-aware backoff (base collector),
  * a per-sub JSON checkpoint at data/reddit_progress.json so a killed
    run resumes without re-firing completed subs' requests.

Items are inserted into the existing raw_items table under
batch_id "reddit-recollect-<run_id>"; URL dedup at insert time drops
anything already collected by the main run, so this is safe to re-run.

Usage:
    uv run python run_reddit_recollect.py
    LOOKBACK_HOURS=168 uv run python run_reddit_recollect.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from src.config import load_config  # noqa: E402
from src.collectors.reddit import RedditCollector  # noqa: E402
from src.storage.db import Database  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reddit_recollect")


async def main() -> None:
    cfg = load_config(str(BASE_DIR / "config" / "config.yaml"), strict=False)
    lookback = int(os.environ.get("LOOKBACK_HOURS", cfg["collection"]["lookback_hours"]))
    since = datetime.now(timezone.utc) - timedelta(hours=lookback)
    batch_id = f"reddit-recollect-{uuid.uuid4().hex[:8]}"

    subs = cfg["sources"]["reddit"].get("subreddits", []) or []
    logger.info("Reddit re-collect: batch=%s, %d subs, lookback=%dh, since=%s",
                batch_id, len(subs), lookback, since.isoformat())

    collector = RedditCollector(cfg)
    items = await collector.collect(since)
    logger.info("Collector returned %d unique items", len(items))

    db = Database(cfg["storage"]["db_path"])
    await db.initialize()
    inserted = 0
    try:
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
    finally:
        await db.close()

    logger.info("Reddit re-collect DONE: %d collected, %d new inserted, batch=%s",
                len(items), inserted, batch_id)
    print(f"RESULT collected={len(items)} inserted={inserted} batch={batch_id}")


if __name__ == "__main__":
    asyncio.run(main())
