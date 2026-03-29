import pytest
import re
from aioresponses import aioresponses
from src.notifiers.slack import SlackNotifier

@pytest.fixture
def slack_config():
    return {
        "notification": {
            "channels": {
                "slack": {
                    "enabled": True,
                    "webhook_url": "https://hooks.slack.com/services/TEST/WEBHOOK"
                }
            }
        }
    }

@pytest.mark.asyncio
async def test_slack_sends_digest(slack_config):
    notifier = SlackNotifier(slack_config)
    with aioresponses() as m:
        m.post("https://hooks.slack.com/services/TEST/WEBHOOK", status=200)
        result = await notifier.send("# Test Digest\nSome content")
    assert result is True

@pytest.mark.asyncio
async def test_slack_handles_failure(slack_config):
    notifier = SlackNotifier(slack_config)
    with aioresponses() as m:
        m.post("https://hooks.slack.com/services/TEST/WEBHOOK", status=500, body="error")
        result = await notifier.send("Test")
    assert result is False

@pytest.mark.asyncio
async def test_slack_missing_webhook():
    notifier = SlackNotifier({"notification": {"channels": {"slack": {}}}})
    result = await notifier.send("Test")
    assert result is False
