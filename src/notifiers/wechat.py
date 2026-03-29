# src/notifiers/wechat.py
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from .base import BaseNotifier

logger = logging.getLogger(__name__)

MAX_WECHAT_BYTES = 4096


class WeChatNotifier(BaseNotifier):
    def __init__(self, config: dict):
        super().__init__(config)
        channels = config.get("notification", {}).get("channels", {})
        self.webhook_url = channels.get("wechat", {}).get("webhook_url", "")

    @property
    def _webhook_key(self) -> str:
        """Extract key from webhook URL for file upload API."""
        parsed = urlparse(self.webhook_url)
        qs = parse_qs(parsed.query)
        keys = qs.get("key", [])
        return keys[0] if keys else ""

    async def send(self, digest: str, compact_digest: str = None) -> bool:
        if not self.webhook_url:
            logger.warning("WeChat webhook URL not configured")
            return False

        # Use compact digest for WeChat (4096 byte limit)
        content = compact_digest or digest

        # Split if still over limit
        messages = self._split_message(content)

        try:
            async with aiohttp.ClientSession() as session:
                for i, msg in enumerate(messages):
                    payload = {
                        "msgtype": "text",
                        "text": {"content": msg}
                    }
                    async with session.post(
                        self.webhook_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.error("WeChat message %d failed: %s %s", i, resp.status, body)
                            return False
                    # Interval between messages
                    if i < len(messages) - 1:
                        await asyncio.sleep(3)

            logger.info("WeChat notification sent (%d messages)", len(messages))
            return True
        except Exception as e:
            logger.error("WeChat notification failed: %s", e)
            return False

    def _split_message(self, content: str) -> list[str]:
        """Split message to fit within WeChat byte limit."""
        encoded = content.encode("utf-8")
        if len(encoded) <= MAX_WECHAT_BYTES:
            return [content]

        LIMIT = MAX_WECHAT_BYTES - 100  # safety margin

        messages = []
        current = ""
        for line in content.split("\n"):
            line_bytes = line.encode("utf-8")
            # If a single line exceeds the limit, hard-split it by bytes
            if len(line_bytes) > LIMIT:
                if current:
                    messages.append(current)
                    current = ""
                # Hard-split the oversized line into LIMIT-byte chunks
                remaining = line
                while remaining:
                    chunk = remaining.encode("utf-8")[:LIMIT].decode("utf-8", errors="ignore")
                    messages.append(chunk)
                    remaining = remaining[len(chunk):]
                continue

            test = current + "\n" + line if current else line
            if len(test.encode("utf-8")) > LIMIT:
                if current:
                    messages.append(current)
                current = line
            else:
                current = test
        if current:
            messages.append(current)
        return messages or [content[:MAX_WECHAT_BYTES // 3]]

    async def send_file(self, file_path: str | Path, summary: str = "") -> bool:
        """Upload a file and send it via WeChat webhook.

        Uses the WeCom webhook upload_media API:
        1. POST file to upload_media endpoint -> get media_id
        2. POST file message with media_id to webhook
        """
        if not self.webhook_url:
            logger.warning("WeChat webhook URL not configured")
            return False

        key = self._webhook_key
        if not key:
            logger.error("Could not extract key from webhook URL")
            return False

        file_path = Path(file_path)
        if not file_path.exists():
            logger.error("File not found: %s", file_path)
            return False

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Upload file to get media_id
                upload_url = (
                    f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
                    f"?key={key}&type=file"
                )
                data = aiohttp.FormData()
                data.add_field(
                    "media",
                    open(file_path, "rb"),
                    filename=file_path.name,
                    content_type="application/octet-stream",
                )
                async with session.post(
                    upload_url, data=data,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    result = await resp.json()
                    if result.get("errcode", 0) != 0:
                        logger.error("WeChat file upload failed: %s", result)
                        return False
                    media_id = result["media_id"]

                logger.info("File uploaded, media_id=%s", media_id)

                # Step 2: Send file message
                payload = {
                    "msgtype": "file",
                    "file": {"media_id": media_id},
                }
                async with session.post(
                    self.webhook_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json()
                    if result.get("errcode", 0) != 0:
                        logger.error("WeChat file send failed: %s", result)
                        return False

                # Step 3: Optionally send a text summary alongside
                if summary:
                    await asyncio.sleep(1)
                    text_payload = {
                        "msgtype": "text",
                        "text": {"content": summary},
                    }
                    async with session.post(
                        self.webhook_url, json=text_payload,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning("WeChat summary text failed")

                logger.info("WeChat file sent: %s", file_path.name)
                return True
        except Exception as e:
            logger.error("WeChat file send failed: %s", e)
            return False
