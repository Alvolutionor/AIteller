# tests/collectors/test_hackernews.py
import re
import pytest
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses
from src.collectors.hackernews import HackerNewsCollector

HN_URL_PATTERN = re.compile(r"https://hn\.algolia\.com/api/v1/search_by_date.*")

@pytest.fixture
def hn_config(sample_config):
    return sample_config

@pytest.mark.asyncio
async def test_hn_collector_parses_response(hn_config):
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = HackerNewsCollector(hn_config)

    mock_response = {
        "hits": [
            {
                "objectID": "123",
                "title": "How I use Claude for coding",
                "url": "https://example.com/claude-coding",
                "author": "testuser",
                "created_at_i": int(datetime.now(timezone.utc).timestamp()),
                "points": 150,
                "num_comments": 42,
                "story_text": "Great article about using LLM",
            }
        ]
    }

    with aioresponses() as m:
        m.get(
            HN_URL_PATTERN,
            payload=mock_response,
            repeat=True,
        )
        items = await collector.collect(since)

    assert len(items) >= 1
    item = items[0]
    assert item.source == "hackernews"
    assert item.title == "How I use Claude for coding"
    assert item.url == "https://example.com/claude-coding"
    assert item.metadata["points"] == 150

@pytest.mark.asyncio
async def test_hn_collector_filters_by_min_points(hn_config):
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    hn_config["sources"] = {"hackernews": {"enabled": True, "min_points": 100}}
    collector = HackerNewsCollector(hn_config)

    mock_response = {
        "hits": [
            {
                "objectID": "1", "title": "Low score LLM post",
                "url": "https://example.com/low", "author": "u",
                "created_at_i": int(datetime.now(timezone.utc).timestamp()),
                "points": 10, "num_comments": 2, "story_text": "",
            },
            {
                "objectID": "2", "title": "High score LLM post",
                "url": "https://example.com/high", "author": "u",
                "created_at_i": int(datetime.now(timezone.utc).timestamp()),
                "points": 200, "num_comments": 50, "story_text": "",
            },
        ]
    }
    with aioresponses() as m:
        m.get(HN_URL_PATTERN, payload=mock_response, repeat=True)
        items = await collector.collect(since)

    assert len(items) == 1
    assert items[0].metadata["points"] == 200

@pytest.mark.asyncio
async def test_hn_collector_handles_api_error(hn_config):
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = HackerNewsCollector(hn_config)
    with aioresponses() as m:
        m.get(HN_URL_PATTERN, status=500, repeat=True)
        items = await collector.collect(since)
    assert items == []
