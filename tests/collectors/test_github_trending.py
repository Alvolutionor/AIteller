import pytest
from datetime import datetime, timezone, timedelta
import re
from aioresponses import aioresponses
from src.collectors.github_trending import GitHubTrendingCollector

MOCK_TRENDING_HTML = '''
<html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/anthropics/claude-code">anthropics / claude-code</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">AI coding assistant using LLM</p>
  <a class="Link--muted" href="/anthropics/claude-code/stargazers">5,234</a>
  <span class="d-inline-block float-sm-right">456 stars today</span>
</article>
</body></html>
'''

@pytest.fixture
def gh_config(sample_config):
    sample_config["sources"]["github_trending"] = {
        "enabled": True,
        "languages": ["python"],
    }
    return sample_config

@pytest.mark.asyncio
async def test_github_parses_trending_page(gh_config):
    collector = GitHubTrendingCollector(gh_config)
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    with aioresponses() as m:
        m.get(re.compile(r"https://github\.com/trending.*"),
              body=MOCK_TRENDING_HTML)
        items = await collector.collect(since)

    assert len(items) == 1
    assert items[0].source == "github_trending"
    assert "claude-code" in items[0].title
    assert items[0].metadata["stars"] == 5234
    assert items[0].metadata["today_stars"] == 456
    assert items[0].metadata["language"] == "python"

@pytest.mark.asyncio
async def test_github_handles_empty_page(gh_config):
    collector = GitHubTrendingCollector(gh_config)
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    with aioresponses() as m:
        m.get(re.compile(r"https://github\.com/trending.*"),
              body="<html><body></body></html>")
        items = await collector.collect(since)

    assert items == []
