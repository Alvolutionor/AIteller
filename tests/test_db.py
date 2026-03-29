# tests/test_db.py
import pytest
from src.storage.db import Database

@pytest.mark.asyncio
async def test_db_creates_tables(db):
    tables = await db.get_tables()
    assert "raw_items" in tables
    assert "processed_items" in tables
    assert "notifications" in tables
    assert "collection_runs" in tables
    assert "extracted_content" in tables
    assert "schema_version" in tables

@pytest.mark.asyncio
async def test_db_schema_version(db):
    version = await db.get_schema_version()
    assert version == 1

@pytest.mark.asyncio
async def test_db_wal_mode(db):
    mode = await db.get_journal_mode()
    assert mode == "wal"

@pytest.mark.asyncio
async def test_insert_and_get_raw_item(db):
    from datetime import datetime, timezone
    item = {
        "source": "hackernews",
        "title": "Test Article",
        "url": "https://example.com/test",
        "author": "testuser",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "content": "Test content",
        "metadata": '{"points": 100}',
        "batch_id": "batch-001",
    }
    item_id = await db.insert_raw_item(item)
    assert item_id is not None

    fetched = await db.get_raw_item_by_url("https://example.com/test")
    assert fetched is not None
    assert fetched["title"] == "Test Article"

@pytest.mark.asyncio
async def test_insert_duplicate_url_skips(db):
    from datetime import datetime, timezone
    item = {
        "source": "hackernews",
        "title": "Test",
        "url": "https://example.com/dup",
        "author": "user",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "content": "",
        "metadata": "{}",
        "batch_id": "batch-001",
    }
    id1 = await db.insert_raw_item(item)
    id2 = await db.insert_raw_item(item)
    assert id1 is not None
    assert id2 is None  # duplicate, skipped
