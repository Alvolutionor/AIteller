# src/main.py
import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import portalocker
from rich.console import Console
from rich.logging import RichHandler

from src.config import load_config, load_feeds
from src.storage.db import Database
from src.utils.llm import LLMClient
from src.utils.extractor import ContentExtractor
from src.collectors.hackernews import HackerNewsCollector
from src.collectors.twitter_api import TwitterAPICollector
from src.collectors.youtube import YouTubeCollector
from src.collectors.bilibili import BilibiliCollector
from src.collectors.reddit import RedditCollector
from src.collectors.github_trending import GitHubTrendingCollector
from src.collectors.arxiv_collector import ArxivCollector
from src.collectors.rss_blogs import RSSBlogsCollector
from src.collectors.hf_papers import HFPapersCollector
from src.processor.dedup import deduplicate
from src.processor.filter import AIFilter
from src.processor.scorer import DeterministicScorer as Scorer
import logging as _logging
logger = _logging.getLogger(__name__)
from src.processor.summarizer import Summarizer
from src.notifiers.slack import SlackNotifier
from src.notifiers.wechat import WeChatNotifier
from src.notifiers.email_notifier import EmailNotifier
from src.report.generator import ReportGenerator
from src.report.weekly import WeeklyReportGenerator

console = Console()
BASE_DIR = Path(__file__).parent.parent


def setup_logging(config: dict):
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file = log_cfg.get("file")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    handlers = [RichHandler(console=console, rich_tracebacks=True)]
    if log_file:
        max_bytes = log_cfg.get("max_size_mb", 50) * 1024 * 1024
        backup_count = log_cfg.get("backup_count", 5)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        ))
    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")


def acquire_lock(lock_path: str):
    """Acquire file lock for concurrent safety. Returns lock file handle."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        return lock_file
    except portalocker.LockException:
        lock_file.close()
        raise RuntimeError(f"Could not acquire lock {lock_path}. Another instance may be running.")


async def _insert_items(db: Database, items: list, batch_id: str) -> int:
    """Insert raw items into DB. Returns count of newly inserted."""
    inserted = 0
    for item in items:
        row = {
            "source": item.source, "title": item.title, "url": item.url,
            "author": item.author,
            "published_at": item.published_at.isoformat(),
            "content": item.content,
            "metadata": json.dumps(item.metadata, ensure_ascii=False),
            "batch_id": batch_id,
        }
        if await db.insert_raw_item(row):
            inserted += 1
    return inserted


async def cmd_collect(config: dict, feeds: dict, dry_run: bool):
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    llm = LLMClient(config, dry_run=dry_run)
    extractor = ContentExtractor(db=db)
    batch_id = str(uuid.uuid4())[:8]

    try:
        await db.insert_collection_run(batch_id)
        lookback = config["collection"]["lookback_hours"]
        since = datetime.now(timezone.utc) - timedelta(hours=lookback)

        console.print(f"[bold]Collecting since {since.isoformat()}...[/bold]")

        # Build collector list
        collectors = []
        source_cfg = config.get("sources", {})
        if source_cfg.get("hackernews", {}).get("enabled"):
            collectors.append(HackerNewsCollector(config))
        if source_cfg.get("reddit", {}).get("enabled"):
            collectors.append(RedditCollector(config))
        if source_cfg.get("github_trending", {}).get("enabled"):
            collectors.append(GitHubTrendingCollector(config))
        if source_cfg.get("arxiv", {}).get("enabled"):
            collectors.append(ArxivCollector(config))
        if source_cfg.get("youtube", {}).get("enabled"):
            collectors.append(YouTubeCollector(config, feeds))
        if source_cfg.get("bilibili", {}).get("enabled"):
            collectors.append(BilibiliCollector(config))
        if source_cfg.get("twitter", {}).get("enabled"):
            collectors.append(TwitterAPICollector(config, feeds))
        if source_cfg.get("rss_blogs", {}).get("enabled"):
            collectors.append(RSSBlogsCollector(config, feeds))
        if source_cfg.get("hf_papers", {}).get("enabled"):
            collectors.append(HFPapersCollector(config))

        # ── Phase 1: Collect + insert each source independently ──
        total_collected = 0
        total_inserted = 0

        # Check which sources already collected in this batch (for resume)
        existing_sources = set()
        async with db._conn.execute(
            "SELECT DISTINCT source FROM raw_items WHERE batch_id = ?", (batch_id,)
        ) as cursor:
            for row in await cursor.fetchall():
                existing_sources.add(row["source"])

        # Also check recent batches — skip sources collected in last 2 hours
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        async with db._conn.execute(
            "SELECT DISTINCT source FROM raw_items WHERE collected_at >= ?", (recent_cutoff,)
        ) as cursor:
            for row in await cursor.fetchall():
                existing_sources.add(row["source"])

        if existing_sources:
            console.print(f"[yellow]Skipping already collected: {', '.join(sorted(existing_sources))}[/yellow]")

        # Filter out already-collected sources
        source_name_map = {
            "HackerNewsCollector": "hackernews", "RedditCollector": "reddit",
            "GitHubTrendingCollector": "github_trending", "ArxivCollector": "arxiv",
            "YouTubeCollector": "youtube", "BilibiliCollector": "bilibili",
            "TwitterAPICollector": "twitter", "RSSBlogsCollector": "rss_blogs",
            "HFPapersCollector": "hf_papers",
        }
        collectors = [c for c in collectors
                      if source_name_map.get(c.__class__.__name__, "") not in existing_sources]

        if not collectors:
            console.print("[bold]All sources already collected, skipping to processing[/bold]")
        else:
            console.print(f"[bold]Running {len(collectors)} collectors...[/bold]")

        async def collect_and_insert(collector):
            """Run one collector and insert its results immediately."""
            name = collector.__class__.__name__
            try:
                items = await collector.collect(since)
                inserted = await _insert_items(db, items, batch_id)
                nonlocal total_collected, total_inserted
                total_collected += len(items)
                total_inserted += inserted
                console.print(
                    f"[green]✓ {name}: {len(items)} collected, {inserted} new "
                    f"(cumulative: {total_collected} collected, {total_inserted} inserted)[/green]"
                )
                return len(items), inserted
            except Exception as e:
                console.print(f"[red]✗ {name} failed: {e}[/red]")
                return 0, 0

        # Run all collectors concurrently — each inserts immediately on completion
        if collectors:
            await asyncio.gather(*[collect_and_insert(c) for c in collectors])

        console.print(f"[bold]Phase 1 done: {total_collected} collected, {total_inserted} new items inserted[/bold]")

        # ── Phase 2: Dedup + Filter + Score (all unprocessed items, not just current batch) ──
        unprocessed = await db.get_unprocessed_raw_items()
        if not unprocessed:
            console.print("No new items to process")
            await db.update_collection_run(batch_id,
                finished_at=datetime.now(timezone.utc).isoformat(),
                total_collected=total_collected, total_passed=0, status="completed")
            return

        from src.collectors.base import RawItem as RI
        raw_items = [
            RI(source=r["source"], title=r["title"], url=r["url"],
               author=r["author"] or "",
               published_at=datetime.fromisoformat(r["published_at"]),
               content=r["content"] or "",
               metadata=json.loads(r["metadata"]) if r["metadata"] else {})
            for r in unprocessed
        ]
        # Dedup: compare against already-processed items
        recent = await db.get_recent_raw_items(since)
        processed_urls = set()
        async with db._conn.execute(
            "SELECT r.url FROM raw_items r JOIN processed_items p ON r.id = p.raw_item_id"
        ) as cursor:
            for row in await cursor.fetchall():
                processed_urls.add(row["url"])
        existing_titles = [r["title"] for r in recent if r["url"] in processed_urls]
        dedup_result = deduplicate(raw_items, existing_titles=existing_titles)
        console.print(f"After dedup: {len(dedup_result.unique_items)} unique items (from {len(raw_items)})")

        # AI Filter (full: pass/reject + category + LLM quality scores)
        ai_filter = AIFilter(llm, batch_size=20)
        passed, rejected = await ai_filter.full_filter(dedup_result.unique_items)
        console.print(f"Full filter: {len(passed)} passed, {len(rejected)} rejected")

        # Score passed items (deterministic + LLM quality scores merged)
        scorer = Scorer()
        summarizer = Summarizer(llm)
        from src.processor.scorer import compute_total_score

        # Batch summarize (Chinese) — much faster than per-item calls
        passed_items = [item for item, _, _ in passed]
        console.print(f"[bold]Batch summarizing {len(passed_items)} items (Chinese)...[/bold]")
        summaries = await summarizer.batch_summarize(passed_items, batch_size=10)

        console.print(f"[bold]Scoring {len(passed)} items...[/bold]")
        for i, (item, category, llm_scores) in enumerate(passed):
            try:
                breakdown = scorer.score_all(item)
                breakdown.update(llm_scores)
                total_score = compute_total_score(breakdown)
                title_zh = summaries[i].get("title_zh", item.title)
                summary_zh = summaries[i].get("summary", "")
                raw = await db.get_raw_item_by_url(item.url)
                if raw:
                    await db.insert_processed_item({
                        "raw_item_id": raw["id"],
                        "passed_filter": True,
                        "filter_reason": f"passed filter: {category}",
                        "score_total": total_score,
                        "score_breakdown": json.dumps(breakdown, ensure_ascii=False),
                        "summary": json.dumps({"title_zh": title_zh, "summary": summary_zh}, ensure_ascii=False),
                        "practice_points": json.dumps([], ensure_ascii=False),
                        "tags": json.dumps([category], ensure_ascii=False),
                        "related_sources": json.dumps([], ensure_ascii=False),
                    })
                    await db.insert_score(
                        raw["id"], True, category, total_score, breakdown
                    )
                if (i + 1) % 50 == 0:
                    console.print(f"  [{i+1}/{len(passed)}] saved")
            except Exception as e:
                logger.error("Failed to score %s: %s", item.url, e)

        # Mark rejected items
        for item, reason in rejected:
            raw = await db.get_raw_item_by_url(item.url)
            if raw:
                await db.insert_processed_item({
                    "raw_item_id": raw["id"],
                    "passed_filter": False,
                    "filter_reason": reason,
                })

        passed_count = len(passed)
        await db.update_collection_run(
            batch_id,
            finished_at=datetime.now(timezone.utc).isoformat(),
            total_collected=total_collected,
            total_passed=passed_count,
            status="completed",
        )
        console.print(f"[bold green]Complete: {passed_count} items passed filter[/bold green]")
    except Exception as e:
        await db.update_collection_run(batch_id, status="failed", error_message=str(e))
        raise
    finally:
        await extractor.close()
        await db.close()


async def cmd_report_daily(config: dict, dry_run: bool, lang: str = "en"):
    """Generate daily PDF report → output/daily/."""
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    llm = LLMClient(config, dry_run=dry_run)

    try:
        items = await db.get_undigested_items()
        if not items:
            console.print("[yellow]No undigested items[/yellow]")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        generator = ReportGenerator(config, lang=lang)
        pdf_path = generator.generate(items, date_str=today)
        console.print(f"[green]Daily PDF generated: {pdf_path}[/green]")

        item_ids = [item["id"] for item in items]
        await db.mark_items_digested(item_ids)
    finally:
        await db.close()


async def cmd_report_weekly(config: dict, top_n: int = 100, lang: str = "en"):
    """Generate weekly PDF report → output/weekly/."""
    db = Database(config["storage"]["db_path"])
    await db.initialize()

    try:
        today = datetime.now(timezone.utc)
        week_ago = today - timedelta(days=7)
        week_start = week_ago.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        all_items = await db.get_weekly_items(week_ago)
        raw_counts = await db.get_weekly_raw_counts(week_ago)
        if not all_items:
            console.print("[yellow]No items found for the past week[/yellow]")
            return

        # Sort by score descending, take top N for PDF
        all_items.sort(key=lambda x: x.get("score_total", 0), reverse=True)
        if top_n and len(all_items) > top_n:
            items = all_items[:top_n]
            console.print(f"[bold]共 {len(all_items)} 条通过筛选，PDF 展示 Top {top_n}[/bold]")
        else:
            items = all_items

        generator = WeeklyReportGenerator(config, lang=lang)
        pdf_path = generator.generate(items, week_start, week_end, raw_counts=raw_counts)
        console.print(f"[green]Weekly PDF generated: {pdf_path}[/green]")
    finally:
        await db.close()


def _find_latest_pdf(directory: str) -> Path | None:
    """Find the most recently modified PDF in a directory."""
    pdf_dir = Path(directory)
    if not pdf_dir.exists():
        return None
    pdfs = sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


async def cmd_send(config: dict, report_type: str):
    """Send the latest daily or weekly PDF via configured channels."""
    output_cfg = config.get("output", {})
    if report_type == "daily":
        pdf_dir = output_cfg.get("daily_dir", "./output/daily")
        subject_prefix = "AIteller 日报"
    else:
        pdf_dir = output_cfg.get("weekly_dir", "./output/weekly")
        subject_prefix = "AIteller 周报"

    pdf_path = _find_latest_pdf(pdf_dir)
    if not pdf_path:
        console.print(f"[red]No PDF found in {pdf_dir}[/red]")
        return

    console.print(f"[bold]Sending: {pdf_path.name}[/bold]")

    channels = config.get("notification", {}).get("channels", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if channels.get("email", {}).get("enabled"):
        notifier = EmailNotifier(config)
        body = f"{subject_prefix} {today}\n详见附件 PDF: {pdf_path.name}"
        subject = f"{subject_prefix} {today}"
        success = await notifier.send(body, compact_digest=body,
                                      attachment=pdf_path, subject=subject)
        console.print(f"[{'green' if success else 'red'}]Email: {'sent' if success else 'failed'}[/]")

    if channels.get("wechat", {}).get("enabled"):
        notifier = WeChatNotifier(config)
        summary = f"{subject_prefix} {today}\n详见附件PDF。"
        success = await notifier.send_file(pdf_path, summary=summary)
        console.print(f"[{'green' if success else 'red'}]WeChat: {'sent' if success else 'failed'}[/]")

    if channels.get("slack", {}).get("enabled"):
        notifier = SlackNotifier(config)
        success = await notifier.send(f"{subject_prefix} {today}", compact_digest="See PDF")
        console.print(f"[{'green' if success else 'red'}]Slack: {'sent' if success else 'failed'}[/]")


async def cmd_rescore(config: dict, dry_run: bool, rescore_all: bool = False):
    """Re-filter + re-score existing raw items without re-collecting."""
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    llm = LLMClient(config, dry_run=dry_run)

    try:
        lookback = config["collection"]["lookback_hours"]
        since = datetime.now(timezone.utc) - timedelta(hours=lookback)

        if rescore_all:
            raw_rows = await db.get_all_raw_items()
            console.print(f"[bold]Rescoring ALL {len(raw_rows)} raw items[/bold]")
        else:
            raw_rows = await db.get_all_raw_items(since)
            console.print(f"[bold]Rescoring {len(raw_rows)} items from last {lookback}h[/bold]")

        if not raw_rows:
            console.print("[yellow]No raw items found[/yellow]")
            return

        # Clear existing processed items for these raw items
        raw_ids = [r["id"] for r in raw_rows]
        await db.clear_processed_items(raw_ids)
        console.print(f"Cleared {len(raw_ids)} existing processed items")

        # Build RawItem objects
        from src.collectors.base import RawItem as RI
        raw_items = [
            RI(source=r["source"], title=r["title"], url=r["url"],
               author=r["author"] or "",
               published_at=datetime.fromisoformat(r["published_at"]),
               content=r["content"] or "",
               metadata=json.loads(r["metadata"]) if r["metadata"] else {})
            for r in raw_rows
        ]

        # URL dedup only (skip expensive O(n²) fuzzy title matching for rescore)
        url_seen = set()
        deduped = []
        for item in raw_items:
            if item.url not in url_seen:
                url_seen.add(item.url)
                deduped.append(item)
        console.print(f"After URL dedup: {len(deduped)} unique items (from {len(raw_items)})")

        # Full filter (category + LLM quality scores)
        from src.processor.filter import AIFilter
        ai_filter = AIFilter(llm, batch_size=20)
        passed, rejected = await ai_filter.full_filter(deduped)
        console.print(f"Full filter: {len(passed)} passed, {len(rejected)} rejected")

        # Batch summarize (Chinese)
        scorer = Scorer()
        summarizer = Summarizer(llm)
        from src.processor.scorer import compute_total_score

        passed_items = [item for item, _, _ in passed]
        console.print(f"[bold]Batch summarizing {len(passed_items)} items (Chinese)...[/bold]")
        summaries = await summarizer.batch_summarize(passed_items, batch_size=10)

        console.print(f"[bold]Scoring {len(passed)} items...[/bold]")
        for i, (item, category, llm_scores) in enumerate(passed):
            try:
                breakdown = scorer.score_all(item)
                breakdown.update(llm_scores)
                total_score = compute_total_score(breakdown)
                title_zh = summaries[i].get("title_zh", item.title)
                summary_zh = summaries[i].get("summary", "")
                raw = await db.get_raw_item_by_url(item.url)
                if raw:
                    await db.insert_processed_item({
                        "raw_item_id": raw["id"],
                        "passed_filter": True,
                        "filter_reason": f"passed filter: {category}",
                        "score_total": total_score,
                        "score_breakdown": json.dumps(breakdown, ensure_ascii=False),
                        "summary": json.dumps({"title_zh": title_zh, "summary": summary_zh}, ensure_ascii=False),
                        "practice_points": json.dumps([], ensure_ascii=False),
                        "tags": json.dumps([category], ensure_ascii=False),
                        "related_sources": json.dumps([], ensure_ascii=False),
                    })
                    await db.insert_score(
                        raw["id"], True, category, total_score, breakdown
                    )
                if (i + 1) % 50 == 0:
                    console.print(f"  [{i+1}/{len(passed)}] saved")
            except Exception as e:
                logger.error("Failed to score %s: %s", item.url, e)

        # Mark rejected
        for item, reason in rejected:
            raw = await db.get_raw_item_by_url(item.url)
            if raw:
                await db.insert_processed_item({
                    "raw_item_id": raw["id"],
                    "passed_filter": False,
                    "filter_reason": reason,
                })

        console.print(f"[bold green]Rescore complete: {len(passed)} passed[/bold green]")
    finally:
        await db.close()


async def cmd_cleanup(config: dict):
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    try:
        storage = config.get("storage", {})
        await db.cleanup_expired(
            raw_days=storage.get("retention_days_raw", 30),
            processed_days=storage.get("retention_days_processed", 90),
        )
        console.print("[green]Cleanup complete[/green]")
    finally:
        await db.close()


async def cmd_status(config: dict):
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    try:
        async with db._conn.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 1"
        ) as cursor:
            run = await cursor.fetchone()
        if run:
            run = dict(run)
            console.print(f"[bold]Last collection:[/bold] {run['started_at']}")
            console.print(f"  Status: {run['status']} | Collected: {run.get('total_collected', '?')} | Passed: {run.get('total_passed', '?')}")
        else:
            console.print("No collection runs found")

        async with db._conn.execute(
            "SELECT channel, MAX(sent_at) as last_sent, status FROM notifications GROUP BY channel"
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            console.print(f"\n[bold]Last notifications:[/bold]")
            for row in rows:
                row = dict(row)
                console.print(f"  {row['channel']}: {row['last_sent']} ({row['status']})")

        async with db._conn.execute(
            "SELECT COUNT(*) as cnt FROM processed_items WHERE passed_filter = 1 AND digested = 0"
        ) as cursor:
            cnt = (await cursor.fetchone())["cnt"]
        console.print(f"\n[bold]Pending digest:[/bold] {cnt} items")
    finally:
        await db.close()


async def cmd_test_source(config: dict, feeds: dict, source_name: str):
    """Test a single source collector."""
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector_map = {
        "hackernews": lambda: HackerNewsCollector(config),
        "reddit": lambda: RedditCollector(config),
        "github": lambda: GitHubTrendingCollector(config),
        "arxiv": lambda: ArxivCollector(config),
        "youtube": lambda: YouTubeCollector(config, feeds),
        "bilibili": lambda: BilibiliCollector(config),
        "twitter": lambda: TwitterAPICollector(config, feeds),
        "rss": lambda: RSSBlogsCollector(config, feeds),
        "hf_papers": lambda: HFPapersCollector(config),
    }
    if source_name not in collector_map:
        console.print(f"[red]Unknown source: {source_name}. Available: {', '.join(collector_map)}[/red]")
        return
    collector = collector_map[source_name]()
    console.print(f"Testing {source_name}...")
    try:
        items = await collector.collect(since)
        console.print(f"[green]Collected {len(items)} items[/green]")
        for item in items[:5]:
            console.print(f"  - [{item.source}] {item.title}")
            console.print(f"    {item.url}")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/red]")


async def cmd_test_notify(config: dict, channel_name: str):
    """Test a single notification channel."""
    notifier_map = {
        "slack": lambda: SlackNotifier(config),
        "wechat": lambda: WeChatNotifier(config),
        "email": lambda: EmailNotifier(config),
    }
    if channel_name not in notifier_map:
        console.print(f"[red]Unknown channel: {channel_name}. Available: {', '.join(notifier_map)}[/red]")
        return
    notifier = notifier_map[channel_name]()
    test_msg = "AIteller test message - if you see this, the notification channel is working!"
    console.print(f"Testing {channel_name}...")
    try:
        success = await notifier.send(test_msg, compact_digest=test_msg)
        console.print(f"[{'green' if success else 'red'}]{channel_name}: {'sent' if success else 'failed'}[/]")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/red]")


def main():
    parser = argparse.ArgumentParser(description="AIteller - AI News Aggregator")
    parser.add_argument("command", choices=[
        "collect", "rescore", "report", "send", "cleanup", "status",
        "test-source", "test-notify",
    ])
    parser.add_argument("target", nargs="?",
                        help="daily|weekly for report/send; all for rescore; source/channel name for test-*")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls and notifications")
    parser.add_argument("--lang", choices=["en", "zh"], default="en",
                        help="Report language (default: en)")
    parser.add_argument("--top", type=int, default=100,
                        help="Max items in weekly PDF (default: 100, 0 = unlimited)")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--feeds", default=str(BASE_DIR / "config" / "feeds.yaml"))
    args = parser.parse_args()

    config = load_config(args.config, strict=False)
    feeds = load_feeds(args.feeds)
    setup_logging(config)

    # Acquire file lock for commands that write to DB
    lock_file = None
    lock_path = os.path.join(os.path.dirname(config["storage"]["db_path"]), "aiteller.lock")
    if args.command in ("collect", "rescore", "report", "cleanup"):
        lock_file = acquire_lock(lock_path)

    try:
        if args.command == "collect":
            asyncio.run(cmd_collect(config, feeds, args.dry_run))

        elif args.command == "rescore":
            rescore_all = (args.target or "").lower() == "all"
            asyncio.run(cmd_rescore(config, args.dry_run, rescore_all=rescore_all))

        elif args.command == "report":
            target = (args.target or "daily").lower()
            if target == "daily":
                asyncio.run(cmd_report_daily(config, args.dry_run, lang=args.lang))
            elif target == "weekly":
                asyncio.run(cmd_report_weekly(config, top_n=args.top, lang=args.lang))
            else:
                console.print(f"[red]Unknown report type: {target}. Use 'daily' or 'weekly'[/red]")

        elif args.command == "send":
            target = (args.target or "daily").lower()
            if target in ("daily", "weekly"):
                asyncio.run(cmd_send(config, target))
            else:
                console.print(f"[red]Unknown send type: {target}. Use 'daily' or 'weekly'[/red]")

        elif args.command == "cleanup":
            asyncio.run(cmd_cleanup(config))
        elif args.command == "status":
            asyncio.run(cmd_status(config))

        elif args.command == "test-source":
            if not args.target:
                console.print("[red]Usage: test-source <source_name>[/red]")
                return
            asyncio.run(cmd_test_source(config, feeds, args.target))
        elif args.command == "test-notify":
            if not args.target:
                console.print("[red]Usage: test-notify <channel_name>[/red]")
                return
            asyncio.run(cmd_test_notify(config, args.target))
    finally:
        if lock_file:
            portalocker.unlock(lock_file)
            lock_file.close()


if __name__ == "__main__":
    main()
