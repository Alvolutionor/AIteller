# tests/processor/test_filter.py
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from src.collectors.base import RawItem
from src.processor.filter import AIFilter

def _make_item(title, content=""):
    return RawItem(
        source="hackernews", title=title, url="https://example.com/test",
        author="u", published_at=datetime.now(timezone.utc),
        content=content, metadata={"points": 100},
    )

@pytest.mark.asyncio
async def test_fast_filter_parses_llm_response():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = '{"results": [{"index": 0, "decision": "pass", "reason": "practical tutorial"}]}'
    f = AIFilter(mock_llm)
    items = [_make_item("Building RAG with Claude: A Tutorial")]
    passed, rejected = await f.fast_filter(items)
    assert len(passed) == 1

@pytest.mark.asyncio
async def test_fast_filter_rejects_news():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = '{"results": [{"index": 0, "decision": "reject", "reason": "pure announcement"}]}'
    f = AIFilter(mock_llm)
    items = [_make_item("OpenAI announces GPT-5")]
    passed, rejected = await f.fast_filter(items)
    assert len(passed) == 0
    assert len(rejected) == 1
