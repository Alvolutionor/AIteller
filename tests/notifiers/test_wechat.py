import pytest
from aioresponses import aioresponses
from src.notifiers.wechat import WeChatNotifier

@pytest.fixture
def wechat_config():
    return {
        "notification": {
            "channels": {
                "wechat": {
                    "enabled": True,
                    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST"
                }
            }
        }
    }

@pytest.mark.asyncio
async def test_wechat_sends_compact_digest(wechat_config):
    notifier = WeChatNotifier(wechat_config)
    with aioresponses() as m:
        m.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST",
               payload={"errcode": 0}, repeat=True)
        result = await notifier.send("Full digest", compact_digest="Short version")
    assert result is True

@pytest.mark.asyncio
async def test_wechat_splits_long_message(wechat_config):
    notifier = WeChatNotifier(wechat_config)
    long_content = "A" * 5000  # Over 4096 bytes
    messages = notifier._split_message(long_content)
    for msg in messages:
        assert len(msg.encode("utf-8")) <= 4096

@pytest.mark.asyncio
async def test_wechat_handles_failure(wechat_config):
    notifier = WeChatNotifier(wechat_config)
    with aioresponses() as m:
        m.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST",
               status=500, body="error")
        result = await notifier.send("Test", compact_digest="Test")
    assert result is False
