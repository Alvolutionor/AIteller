# tests/collectors/test_twitter.py
import pytest
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses

from src.collectors.twitter import TwitterCollector

NITTER1 = "https://nitter.test1.net"
NITTER2 = "https://nitter.test2.net"
RSSHUB = "https://rsshub.test.net"

SAMPLE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>@testuser / Twitter</title>
<item>
  <title>Interesting LLM tweet here</title>
  <link>https://nitter.test1.net/testuser/status/1</link>
  <pubDate>Thu, 20 Mar 2026 10:00:00 GMT</pubDate>
  <description>Full tweet HTML about LLM stuff</description>
</item>
</channel>
</rss>
"""

EMPTY_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel><title>empty</title></channel>
</rss>
"""


@pytest.fixture
def twitter_config(sample_config):
    cfg = dict(sample_config)
    cfg["sources"] = dict(cfg.get("sources", {}))
    cfg["sources"]["twitter"] = {
        "enabled": True,
        "nitter_instances": [NITTER1, NITTER2],
        "rsshub_instance": RSSHUB,
    }
    return cfg


@pytest.fixture
def twitter_feeds():
    return {
        "twitter": [{"name": "Test User", "handle": "testuser"}],
        "mastodon": [],
    }


@pytest.mark.asyncio
async def test_twitter_nitter_success(twitter_config, twitter_feeds):
    """Nitter RSS returns a valid feed → items are parsed into RawItem objects."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    collector = TwitterCollector(twitter_config, feeds=twitter_feeds)

    with aioresponses() as m:
        m.get(f"{NITTER1}/testuser/rss", body=SAMPLE_RSS, content_type="application/rss+xml")
        items = await collector.collect(since)

    assert len(items) == 1
    item = items[0]
    assert item.source == "twitter"
    assert item.title == "Interesting LLM tweet here"
    assert item.url == "https://nitter.test1.net/testuser/status/1"
    assert item.author == "testuser"
    assert item.metadata["handle"] == "testuser"
    assert item.metadata["strategy_used"] == "nitter"
    assert item.published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_twitter_fallback_to_rsshub(twitter_config, twitter_feeds):
    """Nitter fails with 404 → RSSHub is tried and returns items."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    collector = TwitterCollector(twitter_config, feeds=twitter_feeds)

    rsshub_rss = SAMPLE_RSS.replace(
        "https://nitter.test1.net/testuser/status/1",
        "https://rsshub.test.net/twitter/user/testuser/1",
    ).replace(
        "strategy_used", "strategy_used"  # no change needed
    )

    with aioresponses() as m:
        # Both nitter instances fail — register repeat=True so retries don't hit ConnectionRefused
        m.get(f"{NITTER1}/testuser/rss", status=404, repeat=True)
        m.get(f"{NITTER2}/testuser/rss", status=404, repeat=True)
        # RSSHub succeeds
        m.get(
            f"{RSSHUB}/twitter/user/testuser",
            body=rsshub_rss,
            content_type="application/rss+xml",
        )
        items = await collector.collect(since)

    assert len(items) == 1
    item = items[0]
    assert item.source == "twitter"
    assert item.metadata["strategy_used"] == "rsshub"


@pytest.mark.asyncio
async def test_twitter_all_fail_returns_empty(twitter_config, twitter_feeds):
    """All strategies fail → empty list, no exception raised."""
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    collector = TwitterCollector(twitter_config, feeds=twitter_feeds)

    with aioresponses() as m:
        m.get(f"{NITTER1}/testuser/rss", status=503)
        m.get(f"{NITTER2}/testuser/rss", status=503)
        m.get(f"{RSSHUB}/twitter/user/testuser", status=503)
        items = await collector.collect(since)

    assert items == []
