import pytest
import re
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses
from src.collectors.reddit import RedditCollector

@pytest.fixture
def reddit_config(sample_config):
    sample_config["sources"]["reddit"] = {
        "enabled": True,
        "client_id": "test_id",
        "client_secret": "test_secret",
        "subreddits": ["LocalLLaMA"],
        "min_score": 20,
    }
    return sample_config

@pytest.mark.asyncio
async def test_reddit_parses_subreddit_posts(reddit_config):
    collector = RedditCollector(reddit_config)
    now_ts = datetime.now(timezone.utc).timestamp()
    since = datetime.now(timezone.utc) - timedelta(hours=6)

    with aioresponses() as m:
        m.post("https://www.reddit.com/api/v1/access_token",
               payload={"access_token": "test_token", "token_type": "bearer"})
        m.get(re.compile(r"https://oauth\.reddit\.com/r/.*/hot.*"),
              payload={
                  "data": {
                      "children": [{
                          "data": {
                              "title": "New LLM fine-tuning technique",
                              "selftext": "I found a great way to use LLM...",
                              "permalink": "/r/LocalLLaMA/comments/abc/test/",
                              "author": "testuser",
                              "created_utc": now_ts,
                              "score": 150,
                              "num_comments": 42,
                          }
                      }]
                  }
              })
        items = await collector.collect(since)

    assert len(items) == 1
    assert items[0].source == "reddit"
    assert items[0].title == "New LLM fine-tuning technique"
    assert items[0].metadata["score"] == 150
    assert items[0].metadata["subreddit"] == "LocalLLaMA"

@pytest.mark.asyncio
async def test_reddit_filters_by_min_score(reddit_config):
    collector = RedditCollector(reddit_config)
    now_ts = datetime.now(timezone.utc).timestamp()
    since = datetime.now(timezone.utc) - timedelta(hours=6)

    with aioresponses() as m:
        m.post("https://www.reddit.com/api/v1/access_token",
               payload={"access_token": "t", "token_type": "bearer"})
        m.get(re.compile(r"https://oauth\.reddit\.com/r/.*/hot.*"),
              payload={
                  "data": {
                      "children": [{
                          "data": {
                              "title": "Low LLM post", "selftext": "",
                              "permalink": "/r/LocalLLaMA/comments/x/y/",
                              "author": "u", "created_utc": now_ts,
                              "score": 5, "num_comments": 1,
                          }
                      }]
                  }
              })
        items = await collector.collect(since)

    assert len(items) == 0

@pytest.mark.asyncio
async def test_reddit_handles_auth_error(reddit_config):
    collector = RedditCollector(reddit_config)
    since = datetime.now(timezone.utc) - timedelta(hours=6)

    with aioresponses() as m:
        m.post("https://www.reddit.com/api/v1/access_token", status=401)
        items = await collector.collect(since)

    assert items == []
