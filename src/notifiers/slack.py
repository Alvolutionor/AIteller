# src/notifiers/slack.py
import logging
import aiohttp
from .base import BaseNotifier

logger = logging.getLogger(__name__)


class SlackNotifier(BaseNotifier):
    def __init__(self, config: dict):
        super().__init__(config)
        channels = config.get("notification", {}).get("channels", {})
        self.webhook_url = channels.get("slack", {}).get("webhook_url", "")

    async def send(self, digest: str, compact_digest: str = None) -> bool:
        if not self.webhook_url:
            logger.warning("Slack webhook URL not configured")
            return False

        # Split long messages into blocks (Slack limit ~3000 chars per section block)
        blocks = []
        chunks = [digest[i:i+2900] for i in range(0, len(digest), 2900)]
        for chunk in chunks:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk}
            })

        payload = {"blocks": blocks}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        logger.info("Slack notification sent successfully")
                        return True
                    else:
                        body = await resp.text()
                        logger.error("Slack failed: %s %s", resp.status, body)
                        return False
        except Exception as e:
            logger.error("Slack notification failed: %s", e)
            return False
