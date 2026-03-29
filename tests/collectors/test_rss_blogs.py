import pytest
from datetime import datetime, timezone, timedelta
import re
from aioresponses import aioresponses
from src.collectors.rss_blogs import RSSBlogsCollector

MOCK_RSS = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Test Blog</title>
<item>
<title>Building LLM Agents in Production</title>
<link>https://example.com/llm-agents</link>
<pubDate>Thu, 20 Mar 2026 10:00:00 GMT</pubDate>
<description>A tutorial on building production LLM agents</description>
<category>AI</category>
</item>
<item>
<title>Old Post About Cooking</title>
<link>https://example.com/cooking</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate>
<description>Not relevant</description>
</item>
</channel>
</rss>'''

@pytest.fixture
def rss_feeds():
    return {
        "blogs": [
            {"name": "Test Blog", "url": "https://example.com/feed.xml"},
        ],
    }

@pytest.fixture
def rss_config(sample_config):
    return sample_config

@pytest.mark.asyncio
async def test_rss_parses_feed(rss_config, rss_feeds):
    collector = RSSBlogsCollector(rss_config, feeds=rss_feeds)
    since = datetime.now(timezone.utc) - timedelta(days=7)

    with aioresponses() as m:
        m.get("https://example.com/feed.xml", body=MOCK_RSS)
        items = await collector.collect(since)

    assert len(items) >= 1
    assert items[0].source == "rss_blogs"
    assert "LLM" in items[0].title
    assert items[0].metadata["blog_name"] == "Test Blog"

@pytest.mark.asyncio
async def test_rss_filters_by_date(rss_config, rss_feeds):
    collector = RSSBlogsCollector(rss_config, feeds=rss_feeds)
    # Since very recent — should filter out the 2024 post
    since = datetime(2026, 3, 19, tzinfo=timezone.utc)

    with aioresponses() as m:
        m.get("https://example.com/feed.xml", body=MOCK_RSS)
        items = await collector.collect(since)

    # Only the March 2026 post should pass
    for item in items:
        assert item.published_at >= since
