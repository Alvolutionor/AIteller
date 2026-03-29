# tests/collectors/test_bilibili.py
import re
import pytest
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses

from src.collectors.bilibili import BilibiliCollector

BILIBILI_URL_PATTERN = re.compile(
    r"https://api\.bilibili\.com/x/web-interface/search/type.*"
)

NOW_TS = int(datetime.now(timezone.utc).timestamp())

SAMPLE_BILIBILI_RESPONSE = {
    "code": 0,
    "data": {
        "result": [
            {
                "aid": 123,
                "title": "LLM实战教程",
                "author": "AI博主",
                "arcurl": "https://www.bilibili.com/video/BV1test",
                "description": "介绍LLM实战经验",
                "pubdate": NOW_TS,
                "play": 50000,
                "like": 2000,
                "duration": "10:30",
            }
        ]
    },
}


@pytest.fixture
def bili_config(sample_config):
    cfg = dict(sample_config)
    cfg["sources"] = dict(cfg.get("sources", {}))
    cfg["sources"]["bilibili"] = {"enabled": True}
    return cfg


@pytest.mark.asyncio
async def test_bilibili_parses_search_results(bili_config):
    """Bilibili search API response is parsed into RawItem objects."""
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = BilibiliCollector(bili_config)

    with aioresponses() as m:
        m.get(BILIBILI_URL_PATTERN, payload=SAMPLE_BILIBILI_RESPONSE, repeat=True)
        items = await collector.collect(since)

    assert len(items) >= 1
    item = next(i for i in items if "LLM" in i.title)
    assert item.source == "bilibili"
    assert item.title == "LLM实战教程"
    assert item.url == "https://www.bilibili.com/video/BV1test"
    assert item.author == "AI博主"
    assert item.metadata["views"] == 50000
    assert item.metadata["likes"] == 2000
    assert item.metadata["up_name"] == "AI博主"
    assert item.metadata["duration"] == "10:30"
    assert item.published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_bilibili_handles_api_error(bili_config):
    """HTTP 500 on Bilibili API returns empty list without raising."""
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = BilibiliCollector(bili_config)

    with aioresponses() as m:
        m.get(BILIBILI_URL_PATTERN, status=500, repeat=True)
        items = await collector.collect(since)

    assert items == []
