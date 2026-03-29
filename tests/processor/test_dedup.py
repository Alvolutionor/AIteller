# tests/processor/test_dedup.py
import pytest
from datetime import datetime, timezone
from src.collectors.base import RawItem
from src.processor.dedup import deduplicate

def _make_item(title, url, source="hn", points=100):
    return RawItem(
        source=source, title=title, url=url, author="u",
        published_at=datetime.now(timezone.utc), content="",
        metadata={"points": points},
    )

def test_exact_url_dedup():
    items = [
        _make_item("Post A", "https://example.com/a"),
        _make_item("Post A copy", "https://example.com/a"),
    ]
    result = deduplicate(items)
    assert len(result.unique_items) == 1

def test_url_normalization():
    items = [
        _make_item("Post", "https://example.com/a?utm_source=twitter"),
        _make_item("Post", "https://example.com/a"),
    ]
    result = deduplicate(items)
    assert len(result.unique_items) == 1

def test_fuzzy_title_dedup():
    items = [
        _make_item("How I use Claude for coding daily", "https://a.com/1", points=200),
        _make_item("How I use Claude for coding every day", "https://b.com/2", points=50),
    ]
    result = deduplicate(items)
    assert len(result.unique_items) == 1
    assert result.unique_items[0].metadata["points"] == 200  # keeps higher engagement

def test_different_items_not_deduped():
    items = [
        _make_item("Claude vs GPT comparison", "https://a.com/1"),
        _make_item("New RAG framework released", "https://b.com/2"),
    ]
    result = deduplicate(items)
    assert len(result.unique_items) == 2
