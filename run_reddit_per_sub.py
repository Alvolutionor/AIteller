"""Run Reddit collection one subreddit per process to evade session-level
anti-bot tracking on the unauthenticated public JSON endpoint.

Why a separate process per sub: empirical observation in 2026 — Reddit
flags accumulated requests inside a single ClientSession after ~2-4
hits on its /r/<sub>/<sort>.json endpoints, even at conservative 8s
intervals. A fresh process == fresh TCP / TLS / cookie state, which
buys us another ~1 successful request. We cap at hot-only (1 request
per sub) so each spawned process makes exactly one risky call.

Output rows are written directly into the existing aiteller raw_items
table with batch_id="reddit-loop-<run_id>"; the next normal `collect`
call (or the dedup/filter/score pipeline) will pick them up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from src.config import load_config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reddit_loop")


WORKER_SCRIPT = """
import asyncio, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(os.environ['AITELLER_BASE'])
sys.path.insert(0, str(BASE))

from src.config import load_config
from src.collectors.reddit import RedditCollector
from src.storage.db import Database

SUB = os.environ['SUB']
BATCH_ID = os.environ['BATCH_ID']
LOOKBACK_HOURS = int(os.environ.get('LOOKBACK_HOURS', '168'))

async def main():
    cfg = load_config(str(BASE / 'config' / 'config.yaml'), strict=False)
    cfg['sources']['reddit']['subreddits'] = [SUB]
    # Single sub, no sampling, no circuit breaker noise
    cfg['sources']['reddit']['max_subreddits_per_cycle'] = 0
    cfg['sources']['reddit']['circuit_breaker_after'] = 1
    cfg['sources']['reddit']['inter_sub_min'] = 0
    cfg['sources']['reddit']['inter_sub_max'] = 0
    cfg['sources']['reddit']['long_pause_every'] = 0
    cfg['sources']['reddit']['inter_sort_min'] = 0
    cfg['sources']['reddit']['inter_sort_max'] = 0

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    collector = RedditCollector(cfg)
    items = await collector.collect(since)

    db = Database(cfg['storage']['db_path'])
    await db.initialize()
    inserted = 0
    try:
        for item in items:
            row = {
                'source': item.source,
                'title': item.title,
                'url': item.url,
                'author': item.author,
                'published_at': item.published_at.isoformat(),
                'content': item.content,
                'metadata': json.dumps(item.metadata, ensure_ascii=False),
                'batch_id': BATCH_ID,
            }
            if await db.insert_raw_item(row):
                inserted += 1
    finally:
        await db.close()

    # Single line on stdout for the orchestrator to parse
    print(f'RESULT sub={SUB} collected={len(items)} inserted={inserted}')

asyncio.run(main())
"""


def run_one_sub(sub: str, batch_id: str, lookback_hours: int) -> tuple[int, int]:
    """Spawn a fresh process to collect a single subreddit. Returns (collected, inserted)."""
    env = os.environ.copy()
    env["AITELLER_BASE"] = str(BASE_DIR)
    env["SUB"] = sub
    env["BATCH_ID"] = batch_id
    env["LOOKBACK_HOURS"] = str(lookback_hours)

    proc = subprocess.run(
        ["uv", "run", "python", "-c", WORKER_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(BASE_DIR),
    )
    out = proc.stdout.strip().splitlines()
    result_line = next((l for l in out if l.startswith("RESULT ")), "")
    if not result_line:
        logger.warning("r/%s: no RESULT line. stderr: %s",
                       sub, proc.stderr.strip()[-300:])
        return 0, 0
    parts = dict(p.split("=", 1) for p in result_line.split()[1:])
    return int(parts.get("collected", 0)), int(parts.get("inserted", 0))


def load_subreddits() -> list[str]:
    cfg = load_config(str(BASE_DIR / "config" / "config.yaml"), strict=False)
    return cfg["sources"]["reddit"].get("subreddits", []) or []


def main():
    subs = load_subreddits()
    if not subs:
        logger.error("No subreddits in config")
        sys.exit(1)

    batch_id = f"reddit-loop-{uuid.uuid4().hex[:8]}"
    lookback = int(os.environ.get("LOOKBACK_HOURS", "168"))

    inter_min = float(os.environ.get("INTER_SUB_MIN", "20"))
    inter_max = float(os.environ.get("INTER_SUB_MAX", "40"))

    logger.info("Reddit loop start: %d subs, batch=%s, lookback=%dh, "
                "delay=%.0f-%.0fs", len(subs), batch_id, lookback,
                inter_min, inter_max)

    total_collected = 0
    total_inserted = 0
    consecutive_zero = 0
    successes = 0

    for i, sub in enumerate(subs):
        if i > 0:
            delay = random.uniform(inter_min, inter_max)
            logger.info("[%d/%d] sleeping %.0fs before r/%s",
                        i, len(subs), delay, sub)
            try:
                # asyncio.sleep would need an event loop; just use time.sleep here
                import time
                time.sleep(delay)
            except KeyboardInterrupt:
                logger.warning("Interrupted; stopping")
                break

        try:
            collected, inserted = run_one_sub(sub, batch_id, lookback)
        except subprocess.TimeoutExpired:
            logger.warning("r/%s: timeout (>120s) — skipping", sub)
            collected, inserted = 0, 0
        except Exception as e:
            logger.warning("r/%s: orchestrator error: %s", sub, e)
            collected, inserted = 0, 0

        total_collected += collected
        total_inserted += inserted
        if collected > 0:
            successes += 1
            consecutive_zero = 0
            logger.info("✓ r/%s: %d collected, %d inserted (running total: "
                        "%d collected / %d inserted across %d successful subs)",
                        sub, collected, inserted,
                        total_collected, total_inserted, successes)
        else:
            consecutive_zero += 1
            logger.warning("✗ r/%s: 0 collected (consecutive zeros: %d)",
                           sub, consecutive_zero)

        # If we hit a long streak of zeros, the IP is probably burnt for
        # this run; bail early so we do not waste 30 minutes pinging a
        # blocked endpoint.
        if consecutive_zero >= 8:
            logger.warning(
                "8 consecutive zero results — Reddit looks blocked, "
                "stopping early to preserve IP reputation"
            )
            break

    logger.info("Reddit loop done: %d/%d subs successful, %d collected, "
                "%d new inserted, batch=%s",
                successes, len(subs), total_collected, total_inserted, batch_id)


if __name__ == "__main__":
    main()
