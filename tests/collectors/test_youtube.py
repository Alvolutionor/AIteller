# tests/collectors/test_youtube.py
import pytest
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses

from src.collectors.youtube import YouTubeCollector

CHANNEL_ID = "UCtest123"
CHANNEL_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

SAMPLE_YOUTUBE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Test Channel</title>
  <entry>
    <yt:videoId>abc123</yt:videoId>
    <title>Amazing LLM Tutorial</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-03-20T10:00:00+00:00</published>
    <author><name>Test Channel</name></author>
    <media:group>
      <media:description>A great tutorial about LLMs</media:description>
    </media:group>
  </entry>
</feed>
"""


@pytest.fixture
def yt_config(sample_config):
    cfg = dict(sample_config)
    cfg["sources"] = dict(cfg.get("sources", {}))
    cfg["sources"]["youtube"] = {"enabled": True}
    return cfg


@pytest.fixture
def yt_feeds():
    return {
        "youtube": [{"name": "Test Channel", "channel_id": CHANNEL_ID}],
    }


@pytest.mark.asyncio
async def test_youtube_parses_channel_rss(yt_config, yt_feeds):
    """YouTube channel RSS feed is parsed into RawItem objects correctly."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    collector = YouTubeCollector(yt_config, feeds=yt_feeds)

    with aioresponses() as m:
        m.get(CHANNEL_RSS_URL, body=SAMPLE_YOUTUBE_RSS, content_type="application/atom+xml")
        items = await collector.collect(since)

    assert len(items) == 1
    item = items[0]
    assert item.source == "youtube"
    assert item.title == "Amazing LLM Tutorial"
    assert item.url == "https://www.youtube.com/watch?v=abc123"
    assert item.author == "Test Channel"
    assert item.metadata["channel"] == "Test Channel"
    assert item.published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_youtube_handles_missing_channel(yt_config, yt_feeds):
    """404 on channel RSS feed returns empty list without raising."""
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = YouTubeCollector(yt_config, feeds=yt_feeds)

    with aioresponses() as m:
        m.get(CHANNEL_RSS_URL, status=404, repeat=True)
        items = await collector.collect(since)

    assert items == []
