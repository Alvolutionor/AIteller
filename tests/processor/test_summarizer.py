# tests/processor/test_summarizer.py
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from src.collectors.base import RawItem
from src.processor.scorer import ScoreBreakdown
from src.processor.summarizer import Summarizer

def _make_item():
    return RawItem(
        source="hackernews", title="Building RAG Systems",
        url="https://example.com/rag", author="testuser",
        published_at=datetime.now(timezone.utc),
        content="A comprehensive guide to building RAG systems with LLMs",
        metadata={"points": 200},
    )

@pytest.mark.asyncio
async def test_summarize_item():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = "This article covers practical RAG system architecture."
    summarizer = Summarizer(mock_llm)
    item = _make_item()
    score = ScoreBreakdown(practice_depth=0.8)
    result = await summarizer.summarize_item(item, score)
    assert "RAG" in result
    mock_llm.complete.assert_called_once()

@pytest.mark.asyncio
async def test_generate_daily_digest():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = "# AI Daily Digest\n## Highlights\n1. RAG article"
    summarizer = Summarizer(mock_llm)
    items = [{"title": "RAG Guide", "source": "hackernews", "score_total": 4.2, "url": "https://example.com", "summary": "RAG guide"}]
    result = await summarizer.generate_daily_digest(items)
    assert "Highlights" in result or "RAG" in result

@pytest.mark.asyncio
async def test_compact_digest_under_4096_bytes():
    mock_llm = AsyncMock()
    summarizer = Summarizer(mock_llm)
    items = [
        {"title": f"Item {i}", "source": "hn", "score_total": 4.0, "url": f"https://ex.com/{i}", "summary": "test"}
        for i in range(10)
    ]
    result = await summarizer.generate_compact_digest(items)
    assert len(result.encode("utf-8")) <= 4096
    assert "日报" in result
