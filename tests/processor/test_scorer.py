# tests/processor/test_scorer.py
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone, timedelta
from src.collectors.base import RawItem
from src.processor.scorer import Scorer, ScoreBreakdown

def _make_item(title="Test", hours_ago=1, points=100, source="hackernews"):
    return RawItem(
        source=source, title=title, url="https://example.com/test",
        author="u", published_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        content="Some practical content about LLM deployment",
        metadata={"points": points},
    )

def test_score_total_range():
    s = ScoreBreakdown(
        source_authority=1.0, engagement=1.0,
        practice_depth=1.0, recency=1.0, reproducibility=1.0
    )
    assert 0 <= s.total <= 5.0
    assert s.total == 5.0

def test_score_total_zero():
    s = ScoreBreakdown()
    assert s.total == 0.0

def test_recency_24h():
    scorer = Scorer(AsyncMock())
    item = _make_item(hours_ago=12)
    assert scorer._score_recency(item) == 1.0

def test_recency_48h():
    scorer = Scorer(AsyncMock())
    item = _make_item(hours_ago=36)
    assert scorer._score_recency(item) == 0.7

def test_recency_72h():
    scorer = Scorer(AsyncMock())
    item = _make_item(hours_ago=60)
    assert scorer._score_recency(item) == 0.4

@pytest.mark.asyncio
async def test_scorer_calls_llm():
    mock_llm = AsyncMock()
    mock_llm.complete.return_value = '{"practice_depth": 0.8, "reproducibility": 0.7, "rationale": "Good tutorial"}'
    scorer = Scorer(mock_llm)
    item = _make_item()
    result = await scorer.score(item)
    assert result.practice_depth == 0.8
    assert result.reproducibility == 0.7
    assert result.scoring_rationale == "Good tutorial"
    assert 0 <= result.total <= 5.0
