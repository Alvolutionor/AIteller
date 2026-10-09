"""One-shot: generate audio/video → email → hibernate."""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config, load_feeds
from src.storage.db import Database
from src.utils.llm import LLMClient
from src.audio.script_writer import ScriptWriter
from src.audio.tts import TTSEngine
from src.audio.video import VideoMaker
from src.notifiers.email_notifier import EmailNotifier


async def main():
    config = load_config("config/config.yaml", strict=False)

    db = Database(config["storage"]["db_path"])
    await db.initialize()
    llm = LLMClient(config)

    try:
        today = datetime.now(timezone.utc)
        week_ago = today - timedelta(days=7)
        week_start = week_ago.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        all_items = await db.get_weekly_items(week_ago)
        if not all_items:
            print("No items found for the past week")
            return

        all_items.sort(key=lambda x: x.get("score_total", 0), reverse=True)
        items = all_items[:100]
        print(f"Generating audio/video for {len(items)} items ({week_start} ~ {week_end})")

        audio_cfg = config.get("audio", {})
        audio_dir = Path(audio_cfg.get("output", {}).get("audio_dir", "./output/weekly/audio"))
        video_dir = Path(audio_cfg.get("output", {}).get("video_dir", "./output/weekly/video"))

        script_writer = ScriptWriter(llm, config)
        tts_engine = TTSEngine(config)
        video_maker = VideoMaker(config)

        generated_files = []

        for lang in ["zh", "en"]:
            print(f"\n--- {lang.upper()} ---")

            # Script
            print("Generating podcast script...")
            script = await script_writer.generate(items, week_start, week_end, lang=lang)
            if not script:
                print(f"Empty script for {lang}, skipping")
                continue

            script_path = audio_dir / f"weekly_{week_start}_{week_end}_{lang}.txt"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script, encoding="utf-8")
            print(f"Script: {script_path}")

            # TTS
            print("Synthesizing audio...")
            mp3_path = audio_dir / f"weekly_{week_start}_{week_end}_{lang}.mp3"
            await tts_engine.synthesize(script, lang, mp3_path)
            print(f"Audio: {mp3_path}")
            generated_files.append(mp3_path)

            # Cover + video
            try:
                cover_path = video_maker.create_cover(lang, week_start, week_end, len(items), video_dir)
                mp4_path = video_dir / f"weekly_{week_start}_{week_end}_{lang}.mp4"
                video_maker.compose(cover_path, mp3_path, mp4_path)
                print(f"Video: {mp4_path}")
                generated_files.append(mp4_path)
            except Exception as e:
                print(f"Video skipped: {e}")

        # Send email
        if generated_files:
            print("\nSending email to yuguangdang123@gmail.com...")
            notifier = EmailNotifier(config)
            subject = f"AI Practice Weekly Audio {week_start}~{week_end}"
            body = (
                f"AI Practice Weekly ({week_start} ~ {week_end})\n\n"
                f"Attached: {len(generated_files)} audio/video files.\n"
                f"Items covered: {len(items)}\n"
            )
            # Send each file as a separate email (attachment size limits)
            for fpath in generated_files:
                size_mb = fpath.stat().st_size / (1024 * 1024)
                file_subject = f"{subject} - {fpath.name}"
                success = await notifier.send(
                    body + f"\nFile: {fpath.name} ({size_mb:.1f} MB)",
                    subject=file_subject,
                    attachment=fpath,
                )
                status = "sent" if success else "FAILED"
                print(f"  Email ({fpath.name}): {status}")

        print("\nDone! Hibernating in 10 seconds...")

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
    # Hibernate after completion
    import subprocess
    subprocess.run(["shutdown", "/h"], check=False)
