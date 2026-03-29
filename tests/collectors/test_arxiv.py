# tests/collectors/test_arxiv.py
import re
import pytest
from datetime import datetime, timezone, timedelta
from aioresponses import aioresponses

from src.collectors.arxiv_collector import ArxivCollector

ARXIV_URL_PATTERN = re.compile(r"http://export\.arxiv\.org/api/query.*")

RECENT_DATE = "2026-03-20T00:00:00Z"
OLD_DATE = "2020-01-01T00:00:00Z"

SAMPLE_ATOM_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query</title>
  <entry>
    <title>Large Language Models: A Survey</title>
    <id>http://arxiv.org/abs/2403.12345v1</id>
    <published>2026-03-20T00:00:00Z</published>
    <summary>A comprehensive survey of LLM techniques including fine-tuning and deployment.</summary>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <link href="http://arxiv.org/abs/2403.12345v1" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2403.12345v1" title="pdf" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""

OLD_ATOM_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <title>Old Paper from 2020</title>
    <id>http://arxiv.org/abs/2001.00001v1</id>
    <published>2020-01-01T00:00:00Z</published>
    <summary>Very old paper about LLM deployment.</summary>
    <author><name>Old Author</name></author>
    <link href="http://arxiv.org/abs/2001.00001v1" rel="alternate"/>
  </entry>
</feed>
"""


@pytest.fixture
def arxiv_config(sample_config):
    cfg = dict(sample_config)
    cfg["sources"] = dict(cfg.get("sources", {}))
    cfg["sources"]["arxiv"] = {
        "enabled": True,
        "categories": ["cs.CL", "cs.AI", "cs.LG"],
    }
    return cfg


@pytest.mark.asyncio
async def test_arxiv_parses_atom_feed(arxiv_config):
    """Arxiv Atom feed is parsed into RawItem objects with correct fields."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    collector = ArxivCollector(arxiv_config)

    with aioresponses() as m:
        m.get(ARXIV_URL_PATTERN, body=SAMPLE_ATOM_FEED, content_type="application/atom+xml")
        items = await collector.collect(since)

    assert len(items) == 1
    item = items[0]
    assert item.source == "arxiv"
    assert "Large Language Models" in item.title
    assert item.url == "http://arxiv.org/abs/2403.12345v1"
    assert "Alice Smith" in item.author
    assert "cs.CL" in item.metadata["categories"]
    assert item.metadata["pdf_url"] == "http://arxiv.org/pdf/2403.12345v1"
    assert "Alice Smith" in item.metadata["authors"]
    assert item.published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_arxiv_filters_by_date(arxiv_config):
    """Entries published before `since` are excluded."""
    # Set `since` to 2025-01-01, so the 2020 entry should be filtered out
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    collector = ArxivCollector(arxiv_config)

    with aioresponses() as m:
        m.get(ARXIV_URL_PATTERN, body=OLD_ATOM_FEED, content_type="application/atom+xml")
        items = await collector.collect(since)

    assert items == []
