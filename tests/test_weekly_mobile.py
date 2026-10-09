"""Self-check: weekly PDF renders in both desktop and mobile layouts."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report.weekly import MOBILE_FORMAT, WeeklyReportGenerator, WeeklyReportPDF


def _items(n: int) -> list[dict]:
    sources = ["hackernews", "reddit", "github_trending", "arxiv", "rss_blogs"]
    cats = ["coding_agents", "rag_search", "prompt_context", "novelty"]
    return [
        {
            "title": f"Item {i} with a fairly long headline to force wrapping",
            "summary": "A summary sentence repeated to exercise wrapping. " * 6,
            "url": f"https://example.com/{i}",
            "source": sources[i % len(sources)],
            "author": f"author{i}",
            "score_total": 3.0 + (i % 5),
            "tags": [cats[i % len(cats)]],
            "metadata": {"views": 12345, "likes": 678},
        }
        for i in range(n)
    ]


MANY_SOURCES = ["hackernews", "reddit", "github_trending", "arxiv", "rss_blogs",
                "twitter", "youtube", "bilibili", "hf_papers", "media", "linkedin"]


def _items_many_sources(n: int) -> list[dict]:
    items = _items(n)
    for i, item in enumerate(items):
        item["source"] = MANY_SOURCES[i % len(MANY_SOURCES)]
    return items


def _page_count(path: Path) -> int:
    # ponytail: count page objects in the raw bytes instead of adding pypdf.
    return path.read_bytes().count(b"/Type /Page\n")


def _render(mobile: bool, outdir: Path) -> Path:
    gen = WeeklyReportGenerator({"output": {"weekly_dir": str(outdir)}},
                                lang="en", mobile=mobile)
    path = gen.generate(_items(40), "2026-09-17", "2026-09-24",
                        raw_counts={"hackernews": 900, "reddit": 700})
    assert path.exists(), path
    assert path.stat().st_size > 5000, path.stat().st_size
    assert ("_mobile" in path.name) is mobile, path.name
    return path


def _cover_fits(path: Path) -> bool:
    """No text may be drawn below the footer line on any cover page."""
    import pymupdf

    doc = pymupdf.open(path)
    limit = doc[0].rect.height - 8
    for page in doc[:2]:
        for block in page.get_text("blocks"):
            if block[3] > limit:
                return False
    return True


def demo():
    phone = WeeklyReportPDF(lang="en", mobile=True)
    phone.add_page()
    assert round(phone.w) == MOBILE_FORMAT[0], phone.w
    assert phone.h > phone.w, (phone.w, phone.h)

    wide = WeeklyReportPDF(lang="en", mobile=False)
    wide.add_page()
    assert wide.w > wide.h, (wide.w, wide.h)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        desktop_pdf = _render(False, out)
        mobile_pdf = _render(True, out)
        assert desktop_pdf != mobile_pdf

        # A long source table used to overdraw the notes + distribution
        # table, because the cover has auto page break disabled.
        crowded = WeeklyReportGenerator({"output": {"weekly_dir": str(out / "crowded")}},
                                        lang="zh", mobile=True)
        crowded_pdf = crowded.generate(
            _items_many_sources(40), "2026-09-17", "2026-09-24",
            raw_counts={src: 500 for src in MANY_SOURCES})
        assert _cover_fits(crowded_pdf), "cover content overflowed the mobile page"

        d_pages = _page_count(desktop_pdf)
        m_pages = _page_count(mobile_pdf)
        assert d_pages > 1, d_pages
        # Single column on a narrower page must need more pages than the
        # two-column landscape layout for the same items.
        assert m_pages > d_pages, (d_pages, m_pages)
        print(f"OK desktop={d_pages}p mobile={m_pages}p")


if __name__ == "__main__":
    demo()
