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
from src.collectors.media import MediaCollector
from src.collectors.linkedin import LinkedInCollector
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
from src.audio.script_writer import ScriptWriter
from src.audio.direct_script import build_direct_script
from src.audio.tts import TTSEngine, make_tts_engine
from src.audio.video import VideoMaker
from src.audio.uploader import Uploader

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
        if source_cfg.get("media", {}).get("enabled"):
            collectors.append(MediaCollector(config, feeds))
        if source_cfg.get("linkedin", {}).get("enabled"):
            collectors.append(LinkedInCollector(config, feeds))

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
            "MediaCollector": "media",
            "LinkedInCollector": "linkedin",
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
                total_score = compute_total_score(breakdown, category=category, source=item.source)
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


def _select_weekly_top_items(all_items: list, top_n: int, config: dict) -> list:
    """Apply AI keyword gate, title fuzzy dedup, source caps + floors,
    and return the Top-N items for weekly PDF / video. Shared by
    cmd_report_weekly and cmd_audio_weekly so the PDF and the slideshow
    cover the same content."""
    import re as _re

    if not all_items:
        return []

    ai_kws = config.get("keywords", {}).get("primary", []) or []
    anchor_terms = {
        "ai", "llm", "gpt", "rag", "mcp",
        "claude", "gemini", "deepseek", "llama", "qwen", "mistral",
        "anthropic", "openai", "agent", "embedding", "fine-tun",
        "transformer", "diffusion", "midjourney", "comfyui",
        "perplexity", "cursor", "copilot", "huggingface", "ollama",
        "langchain", "llamaindex", "prompt", "model context",
        "vector database",
        "人工智能", "大模型", "智能体", "提示词", "向量",
    }
    kw_set = set(k.lower() for k in ai_kws) | anchor_terms
    short_kws = {k for k in kw_set if len(k) < 4}
    long_kws = {k for k in kw_set if len(k) >= 4}
    short_re = _re.compile(
        r"(?<![a-z0-9])(?:" +
        "|".join(_re.escape(k) for k in short_kws) +
        r")(?![a-z0-9])",
        _re.IGNORECASE,
    ) if short_kws else None

    def has_ai_signal(item):
        text = (item.get("title", "") + " " + (item.get("content") or "")).lower()
        if any(kw in text for kw in long_kws):
            return True
        if short_re is not None and short_re.search(text):
            return True
        return False

    before_gate = len(all_items)
    items = [it for it in all_items if has_ai_signal(it)]
    if len(items) < before_gate:
        console.print(
            f"[yellow]AI keyword gate: {before_gate} → {len(items)} "
            f"(dropped {before_gate - len(items)} off-topic)[/yellow]"
        )

    items.sort(key=lambda x: x.get("score_total", 0), reverse=True)

    # Title fuzzy dedup
    def _norm_title(s):
        s = (s or "").lower()
        s = _re.sub(r"[\s\-_:.,!?'\"/()\[\]]+", "", s)
        return s[:120]
    seen_titles = set()
    deduped = []
    for it in items:
        key = _norm_title(it.get("title", ""))
        if key and key in seen_titles:
            continue
        if key:
            seen_titles.add(key)
        deduped.append(it)
    if len(deduped) < len(items):
        console.print(
            f"[yellow]Title dedup: {len(items)} → {len(deduped)} "
            f"(dropped {len(items) - len(deduped)} near-duplicate titles)[/yellow]"
        )
    items = deduped

    if not top_n or len(items) <= top_n:
        return items

    source_caps_pct = {
        "bilibili": 0.10,
        "youtube": 0.15,
        "github_trending": 0.05,
    }
    source_floors_pct = {
        "media": 0.05,
        "linkedin": 0.03,
    }
    caps_abs = {s: max(1, int(top_n * pct)) for s, pct in source_caps_pct.items()}
    floors_abs = {s: max(1, int(top_n * pct)) for s, pct in source_floors_pct.items()}

    picked = []
    picked_urls = set()
    counts = {}

    # Phase 0: floors
    for src, floor in floors_abs.items():
        taken = 0
        for it in items:
            if taken >= floor:
                break
            if it.get("source") != src:
                continue
            if it.get("url") in picked_urls:
                continue
            picked.append(it)
            picked_urls.add(it.get("url"))
            counts[src] = counts.get(src, 0) + 1
            taken += 1

    # Phase 1: by score, respect caps
    for it in items:
        if len(picked) >= top_n:
            break
        if it.get("url") in picked_urls:
            continue
        src = it.get("source", "")
        if src in caps_abs and counts.get(src, 0) >= caps_abs[src]:
            continue
        picked.append(it)
        picked_urls.add(it.get("url"))
        counts[src] = counts.get(src, 0) + 1

    # Phase 2: backfill
    if len(picked) < top_n:
        for it in items:
            if len(picked) >= top_n:
                break
            if it.get("url") in picked_urls:
                continue
            picked.append(it)
            picked_urls.add(it.get("url"))

    cap_summary = ", ".join(f"{s}≤{caps_abs[s]}" for s in caps_abs)
    floor_summary = ", ".join(f"{s}≥{floors_abs[s]}" for s in floors_abs)
    console.print(
        f"[bold]Top {len(picked)} from {len(items)}[/bold] "
        f"(caps: {cap_summary} | floors: {floor_summary})"
    )
    return picked


def _weekly_window() -> tuple[datetime, datetime, datetime | None]:
    end = os.environ.get("WEEKLY_END")
    until = (datetime.fromisoformat(end).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
             if end else None)
    today = until or datetime.now(timezone.utc)
    window_days = int(os.environ.get("WEEKLY_WINDOW_DAYS", "7"))
    return today - timedelta(days=window_days), today, until


async def cmd_report_weekly(config: dict, top_n: int = 100, lang: str = "en",
                            mobile: bool = False):
    """Generate weekly PDF report → output/weekly/."""
    db = Database(config["storage"]["db_path"])
    await db.initialize()

    try:
        week_ago, today, until = _weekly_window()
        week_start = week_ago.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        all_items = await db.get_weekly_items(week_ago, until)
        raw_counts = await db.get_weekly_raw_counts(week_ago, until)
        if not all_items:
            console.print("[yellow]No items found for the past week[/yellow]")
            return

        items = _select_weekly_top_items(all_items, top_n, config)

        for is_mobile in ([False, True] if mobile else [False]):
            generator = WeeklyReportGenerator(config, lang=lang, mobile=is_mobile)
            pdf_path = generator.generate(items, week_start, week_end, raw_counts=raw_counts)
            label = "Mobile PDF" if is_mobile else "Weekly PDF"
            console.print(f"[green]{label} generated: {pdf_path}[/green]")
    finally:
        await db.close()


async def cmd_audio_weekly(config: dict, top_n: int = 100, lang: str = "both",
                           upload: bool = False, script_mode: str = "direct",
                           video_mode: str = "static"):
    """Generate weekly audio (MP3) + video (MP4) from weekly report data.

    script_mode: "direct" (default) = read title+summary from DB (no LLM).
                 "llm" = LLM-written podcast script.
    video_mode:  "static" (default) = single cover image + audio.
                 "slideshow" = one slide per item, timed to each segment's audio.
    """
    db = Database(config["storage"]["db_path"])
    await db.initialize()
    llm = LLMClient(config)

    try:
        week_ago, today, until = _weekly_window()
        week_start = week_ago.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        all_items = await db.get_weekly_items(week_ago, until)
        if not all_items:
            console.print("[yellow]No items found for the past week[/yellow]")
            return

        # Apply the same AI gate / dedup / source caps + floors as the
        # weekly PDF so the slideshow video and the PDF cover the
        # same content.
        items = _select_weekly_top_items(all_items, top_n, config)

        console.print(f"[bold]Generating audio/video for {len(items)} items ({week_start} ~ {week_end})[/bold]")

        langs = ["zh", "en"] if lang == "both" else [lang]
        audio_cfg = config.get("audio", {})
        audio_dir = Path(audio_cfg.get("output", {}).get("audio_dir", "./output/weekly/audio"))
        video_dir = Path(audio_cfg.get("output", {}).get("video_dir", "./output/weekly/video"))

        script_writer = ScriptWriter(llm, config)
        tts_engine = make_tts_engine(config)
        video_maker = VideoMaker(config)

        video_paths = []

        for cur_lang in langs:
            console.print(f"\n[bold cyan]--- {cur_lang.upper()} ---[/bold cyan]")

            if video_mode == "slideshow":
                from src.audio.segmented_script import build_segments
                from src.audio.slideshow import build_slideshow
                console.print("Building per-item slideshow (segments + slides + concat)...")
                segs = build_segments(items, week_start, week_end, lang=cur_lang,
                                      max_summary_chars=80, skip_novelty=True)
                console.print(f"[green]{len(segs)} segments[/green]")
                voice = audio_cfg.get("voices", {}).get(cur_lang,
                            "zh-CN-XiaoxiaoNeural" if cur_lang == "zh" else "en-US-GuyNeural")
                rate = audio_cfg.get("tts_rate", "+0%")
                res = tuple(audio_cfg.get("video", {}).get("resolution", [1920, 1080]))
                work = video_dir / f"_work_{week_start}_{week_end}_{cur_lang}"
                mp4_path = video_dir / f"slideshow_{week_start}_{week_end}_{cur_lang}.mp4"
                cover_path = video_dir / f"cover_{week_start}_{week_end}_{cur_lang}.png"
                await build_slideshow(segs, voice, rate, res, week_start, week_end,
                                      cur_lang, work, mp4_path,
                                      cover_out_path=cover_path)
                console.print(f"[green]Slideshow: {mp4_path}[/green]")
                console.print(f"[green]Cover:     {cover_path}[/green]")
                video_paths.append(mp4_path)
                continue

            # Step 1: Generate script
            if script_mode == "direct":
                console.print("Building direct script from DB (no LLM)...")
                script = build_direct_script(items, week_start, week_end, lang=cur_lang)
            else:
                console.print("Generating podcast script...")
                script = await script_writer.generate(items, week_start, week_end, lang=cur_lang)
            if not script:
                console.print(f"[red]Empty script for {cur_lang}, skipping[/red]")
                continue

            # Save script for reference
            script_path = audio_dir / f"weekly_{week_start}_{week_end}_{cur_lang}.txt"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script, encoding="utf-8")
            console.print(f"[green]Script saved: {script_path}[/green]")

            # Step 2: TTS → MP3
            console.print("Synthesizing audio (Edge TTS)...")
            mp3_path = audio_dir / f"weekly_{week_start}_{week_end}_{cur_lang}.mp3"
            await tts_engine.synthesize(script, cur_lang, mp3_path)
            console.print(f"[green]Audio: {mp3_path}[/green]")

            # Step 3: Cover image → PNG
            console.print("Creating cover image...")
            try:
                cover_path = video_maker.create_cover(
                    cur_lang, week_start, week_end, len(items), video_dir,
                    items=items,
                )
                console.print(f"[green]Cover: {cover_path}[/green]")

                # Step 4: Compose video → MP4
                console.print("Composing video (ffmpeg)...")
                mp4_path = video_dir / f"weekly_{week_start}_{week_end}_{cur_lang}.mp4"
                video_maker.compose(cover_path, mp3_path, mp4_path)
                console.print(f"[green]Video: {mp4_path}[/green]")
                video_paths.append(mp4_path)
            except RuntimeError as e:
                console.print(f"[yellow]Video skipped ({e}). MP3 is still available.[/yellow]")

        # Step 5: Upload helper
        if upload and video_paths:
            uploader = Uploader(config)
            title_zh = f"AI 实践周报 {week_start}~{week_end}"
            title_en = f"AI Practice Weekly {week_start}~{week_end}"
            title = title_zh if "zh" in langs else title_en
            desc = f"Weekly AI engineering practice digest covering {week_start} to {week_end}."
            uploader.open_upload_pages(video_paths, title, desc)

        console.print(f"\n[bold green]Audio/video generation complete![/bold green]")
    finally:
        await db.close()


def _find_latest_pdf(directory: str, pattern: str = "*.pdf") -> Path | None:
    """Find the most recently modified PDF matching pattern in a directory."""
    pdf_dir = Path(directory)
    if not pdf_dir.exists():
        return None
    pdfs = sorted(pdf_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


# English-only recipients — get English PDF; everyone else gets Chinese PDF
_EN_ONLY_RECIPIENTS = {"yuguangdang123@gmail.com"}


async def cmd_send(config: dict, report_type: str):
    """Send the latest daily or weekly PDF via configured channels.

    For weekly reports, English PDF goes to _EN_ONLY_RECIPIENTS,
    Chinese PDF goes to all other subscribers.
    """
    output_cfg = config.get("output", {})
    if report_type == "daily":
        pdf_dir = output_cfg.get("daily_dir", "./output/daily")
        subject_prefix = "AIteller 日报"
    else:
        pdf_dir = output_cfg.get("weekly_dir", "./output/weekly")
        subject_prefix = "AIteller 周报"

    channels = config.get("notification", {}).get("channels", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if channels.get("email", {}).get("enabled"):
        notifier = EmailNotifier(config)
        all_recipients = notifier.recipients

        if report_type == "weekly":
            # Split recipients: EN-only vs Chinese
            en_recipients = [r for r in all_recipients if r in _EN_ONLY_RECIPIENTS]
            zh_recipients = [r for r in all_recipients if r not in _EN_ONLY_RECIPIENTS]

            # Send Chinese PDF to zh recipients
            if zh_recipients:
                zh_pdf = _find_latest_pdf(pdf_dir, "AI实践周报_*.pdf")
                if zh_pdf:
                    console.print(f"[bold]Sending (zh): {zh_pdf.name} → {len(zh_recipients)} recipients[/bold]")
                    body = f"AIteller 周报 {today}\n详见附件 PDF: {zh_pdf.name}"
                    success = await notifier.send(body, compact_digest=body,
                                                  attachment=zh_pdf, subject=f"AIteller 周报 {today}",
                                                  recipients_override=zh_recipients)
                    console.print(f"[{'green' if success else 'red'}]Email (zh): {'sent' if success else 'failed'}[/]")
                else:
                    console.print("[red]No Chinese weekly PDF found[/red]")

            # Send English PDF to en recipients
            if en_recipients:
                en_pdf = _find_latest_pdf(pdf_dir, "AI_Practice_Weekly_*.pdf")
                if en_pdf:
                    console.print(f"[bold]Sending (en): {en_pdf.name} → {len(en_recipients)} recipients[/bold]")
                    body = f"AIteller Weekly {today}\nSee attached PDF: {en_pdf.name}"
                    success = await notifier.send(body, compact_digest=body,
                                                  attachment=en_pdf, subject=f"AIteller Weekly {today}",
                                                  recipients_override=en_recipients)
                    console.print(f"[{'green' if success else 'red'}]Email (en): {'sent' if success else 'failed'}[/]")
                else:
                    console.print("[red]No English weekly PDF found[/red]")
        else:
            # Daily: send same PDF to everyone (existing behavior)
            pdf_path = _find_latest_pdf(pdf_dir)
            if not pdf_path:
                console.print(f"[red]No PDF found in {pdf_dir}[/red]")
            else:
                console.print(f"[bold]Sending: {pdf_path.name}[/bold]")
                body = f"{subject_prefix} {today}\n详见附件 PDF: {pdf_path.name}"
                subject = f"{subject_prefix} {today}"
                success = await notifier.send(body, compact_digest=body,
                                              attachment=pdf_path, subject=subject)
                console.print(f"[{'green' if success else 'red'}]Email: {'sent' if success else 'failed'}[/]")

    if channels.get("wechat", {}).get("enabled"):
        # WeChat always gets Chinese PDF
        pdf_path = _find_latest_pdf(pdf_dir, "AI实践周报_*.pdf") if report_type == "weekly" else _find_latest_pdf(pdf_dir)
        if pdf_path:
            notifier = WeChatNotifier(config)
            summary = f"{subject_prefix} {today}\n详见附件PDF。"
            success = await notifier.send_file(pdf_path, summary=summary)
            console.print(f"[{'green' if success else 'red'}]WeChat: {'sent' if success else 'failed'}[/]")

    if channels.get("slack", {}).get("enabled"):
        notifier = SlackNotifier(config)
        success = await notifier.send(f"{subject_prefix} {today}", compact_digest="See PDF")
        console.print(f"[{'green' if success else 'red'}]Slack: {'sent' if success else 'failed'}[/]")


async def cmd_rescore(config: dict, dry_run: bool, rescore_all: bool = False):
    """Re-filter + re-score raw items, persisting each batch immediately
    so an interrupted run can be resumed without redoing completed work.
    """
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

        # Resume support: skip raw_ids that already have a processed_items
        # row. The previous behavior of clear_processed_items first is gone
        # — restarting now picks up where the last run left off.
        already_done = await db.get_processed_raw_ids(
            since=None if rescore_all else since
        )
        if already_done:
            console.print(
                f"[yellow]Skipping {len(already_done)} raw_ids already "
                f"in processed_items (resume mode)[/yellow]"
            )
            raw_rows = [r for r in raw_rows if r["id"] not in already_done]
            if not raw_rows:
                console.print("[bold green]Nothing to do — all already processed[/bold green]")
                return

        # Build RawItem objects from the unprocessed rows
        from src.collectors.base import RawItem as RI
        raw_items = [
            RI(source=r["source"], title=r["title"], url=r["url"],
               author=r["author"] or "",
               published_at=datetime.fromisoformat(r["published_at"]),
               content=r["content"] or "",
               metadata=json.loads(r["metadata"]) if r["metadata"] else {})
            for r in raw_rows
        ]

        # URL dedup
        url_seen = set()
        deduped = []
        for item in raw_items:
            if item.url not in url_seen:
                url_seen.add(item.url)
                deduped.append(item)
        console.print(f"After URL dedup: {len(deduped)} unique items (from {len(raw_items)})")

        # Per-batch atomic pipeline: filter LLM call → score → summarize →
        # insert. Persisting after every batch means a kill at batch N
        # only loses the in-flight batches, not the work already done.
        from src.processor.filter import AIFilter
        from src.processor.scorer import compute_total_score

        ai_filter = AIFilter(llm, batch_size=20)
        scorer = Scorer()
        summarizer = Summarizer(llm)

        # Aggregate counters that the closure mutates
        progress = {"passed": 0, "rejected": 0, "saved": 0, "errors": 0}

        async def persist_batch(batch_idx, batch_passed, batch_rejected):
            # Summarize the passed items in this batch only — keeps the
            # batch self-contained so killing now leaves nothing dangling.
            if batch_passed:
                items_to_summarize = [it for it, _, _ in batch_passed]
                try:
                    summaries = await summarizer.batch_summarize(
                        items_to_summarize, batch_size=10,
                    )
                except Exception as e:
                    logger.warning(
                        "batch %d summarize failed (%s) — using titles as fallback",
                        batch_idx + 1, e,
                    )
                    summaries = [
                        {"title_zh": it.title, "summary": ""}
                        for it in items_to_summarize
                    ]
            else:
                summaries = []

            for i, (item, category, llm_scores) in enumerate(batch_passed):
                try:
                    breakdown = scorer.score_all(item)
                    breakdown.update(llm_scores)
                    total_score = compute_total_score(breakdown, category=category, source=item.source)
                    title_zh = summaries[i].get("title_zh", item.title) if i < len(summaries) else item.title
                    summary_zh = summaries[i].get("summary", "") if i < len(summaries) else ""
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
                        progress["saved"] += 1
                except Exception as e:
                    progress["errors"] += 1
                    logger.error("persist passed for %s: %s", item.url, e)

            for item, reason in batch_rejected:
                try:
                    raw = await db.get_raw_item_by_url(item.url)
                    if raw:
                        await db.insert_processed_item({
                            "raw_item_id": raw["id"],
                            "passed_filter": False,
                            "filter_reason": reason,
                        })
                        progress["saved"] += 1
                except Exception as e:
                    progress["errors"] += 1
                    logger.error("persist rejected for %s: %s", item.url, e)

            progress["passed"] += len(batch_passed)
            progress["rejected"] += len(batch_rejected)
            console.print(
                f"  batch {batch_idx + 1}: +{len(batch_passed)} passed, "
                f"+{len(batch_rejected)} rejected "
                f"(running: {progress['passed']}p / {progress['rejected']}r / "
                f"{progress['saved']} saved)"
            )

        concurrency = int(os.environ.get("RESCORE_CONCURRENCY", "5"))
        await ai_filter.full_filter(deduped, concurrency=concurrency, on_batch_done=persist_batch)

        console.print(
            f"[bold green]Rescore complete: {progress['passed']} passed, "
            f"{progress['rejected']} rejected, {progress['saved']} rows "
            f"persisted, {progress['errors']} errors[/bold green]"
        )
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
        "media": lambda: MediaCollector(config, feeds),
        "linkedin": lambda: LinkedInCollector(config, feeds),
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
        "collect", "rescore", "report", "audio", "send", "cleanup", "status",
        "test-source", "test-notify",
    ])
    parser.add_argument("target", nargs="?",
                        help="daily|weekly for report/send/audio; all for rescore; source/channel name for test-*")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls and notifications")
    parser.add_argument("--mobile", action="store_true",
                        help="Also emit a phone-friendly single-column PDF")
    parser.add_argument("--lang", choices=["en", "zh", "both"], default="en",
                        help="Report language (default: en, 'both' for audio)")
    parser.add_argument("--top", type=int, default=100,
                        help="Max items in weekly PDF (default: 100, 0 = unlimited)")
    parser.add_argument("--upload", action="store_true",
                        help="Open browser upload pages after audio/video generation")
    parser.add_argument("--script-mode", choices=["llm", "direct"], default="direct",
                        help="audio: 'direct' reads title+summary from DB verbatim (no LLM, no token limit); 'llm' writes a podcast script")
    parser.add_argument("--video-mode", choices=["static", "slideshow"], default="slideshow",
                        help="audio: 'slideshow' (default) = one slide per item with rich cover; 'static' = single cover + audio")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "config.yaml"))
    parser.add_argument("--feeds", default=str(BASE_DIR / "config" / "feeds.yaml"))
    args = parser.parse_args()

    config = load_config(args.config, strict=False)
    feeds = load_feeds(args.feeds)
    setup_logging(config)

    # Acquire file lock for commands that write to DB
    lock_file = None
    lock_path = os.path.join(os.path.dirname(config["storage"]["db_path"]), "aiteller.lock")
    if args.command in ("collect", "rescore", "report", "audio", "cleanup"):
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
                asyncio.run(cmd_report_weekly(config, top_n=args.top, lang=args.lang,
                                              mobile=args.mobile))
            else:
                console.print(f"[red]Unknown report type: {target}. Use 'daily' or 'weekly'[/red]")

        elif args.command == "audio":
            target = (args.target or "weekly").lower()
            if target == "weekly":
                audio_lang = args.lang if args.lang != "en" else "both"
                asyncio.run(cmd_audio_weekly(config, top_n=args.top, lang=audio_lang,
                                             upload=args.upload, script_mode=args.script_mode,
                                             video_mode=args.video_mode))
            else:
                console.print(f"[red]Unknown audio type: {target}. Use 'weekly'[/red]")

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
