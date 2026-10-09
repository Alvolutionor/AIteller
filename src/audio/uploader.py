"""Semi-automatic upload helper — opens browser tabs and prints metadata."""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

UPLOAD_URLS = {
    "bilibili": "https://member.bilibili.com/platform/upload/video/frame",
    "youtube": "https://studio.youtube.com/channel/upload",
}


class Uploader:
    def __init__(self, config: dict):
        audio_cfg = config.get("audio", {})
        upload_cfg = audio_cfg.get("upload", {})
        self.targets = upload_cfg.get("targets", ["bilibili", "youtube"])
        self.tags = upload_cfg.get("tags", ["AI", "LLM", "weekly", "engineering practice"])

    def open_upload_pages(
        self,
        video_paths: list[Path],
        title: str,
        description: str,
    ) -> None:
        """Open upload pages in browser and print copy-paste metadata."""
        print("\n" + "=" * 60)
        print("UPLOAD HELPER")
        print("=" * 60)

        print(f"\nTitle:       {title}")
        print(f"Tags:        {', '.join(self.tags)}")
        print(f"Description: {description[:200]}")

        print("\nVideo files:")
        for path in video_paths:
            print(f"  {path.resolve()}")

        print("\nOpening upload pages...")
        for target in self.targets:
            url = UPLOAD_URLS.get(target)
            if url:
                try:
                    webbrowser.open(url)
                    print(f"  Opened: {target} ({url})")
                except Exception as e:
                    print(f"  Failed to open {target}: {e}")
                    print(f"  Manual URL: {url}")
            else:
                logger.warning("Unknown upload target: %s", target)

        print("\n" + "=" * 60)
