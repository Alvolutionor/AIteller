# tests/conftest.py
import os
import pytest
import pytest_asyncio
from src.storage.db import Database

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()

@pytest.fixture
def sample_config():
    return {
        "collection": {"interval_hours": 3, "lookback_hours": 6, "max_items_per_source": 100},
        "keywords": {"primary": ["LLM", "GPT"], "boost": ["tutorial"]},
        "sources": {
            "hackernews": {"enabled": True, "min_points": 50},
            "reddit": {"enabled": True, "subreddits": ["LocalLLaMA"], "min_score": 20},
        },
        "llm": {
            "primary": "claude",
            "fallback": "openai",
            "claude": {"api_key": "test-key", "fast_model": "claude-haiku-4-5-20251001", "standard_model": "claude-sonnet-4-6"},
            "openai": {"api_key": "test-key", "fast_model": "gpt-4o-mini", "standard_model": "gpt-4o"},
            "daily_token_budget": 500000,
        },
    }

@pytest.fixture
def sample_feeds():
    return {
        "twitter": [{"name": "Test", "handle": "test"}],
        "mastodon": [],
        "youtube": [{"name": "Test Channel", "channel_id": "UC123"}],
        "bilibili": [],
        "blogs": [{"name": "Test Blog", "url": "https://example.com/feed"}],
    }
