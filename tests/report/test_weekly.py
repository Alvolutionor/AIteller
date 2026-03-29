import json

from src.report.weekly import (
    _category_distribution_rows,
    _clean_pdf_text,
    _extract_display_content,
    _group_items_by_tier_and_category,
)


def _item(source: str, category: str, score: float, title: str = "Title", summary: str = "摘要") -> dict:
    return {
        "source": source,
        "score_total": score,
        "title": title,
        "summary": json.dumps({"title_zh": title, "summary": summary}, ensure_ascii=False),
        "tags": json.dumps([category], ensure_ascii=False),
        "metadata": json.dumps({}, ensure_ascii=False),
    }


def test_category_distribution_rows_counts_by_source_and_total():
    items = [
        _item("reddit", "personal_workflow", 5.8),
        _item("reddit", "personal_workflow", 5.1),
        _item("bilibili", "personal_workflow", 5.4),
        _item("youtube", "tool_comparison", 4.9),
    ]

    rows = _category_distribution_rows(items, ["reddit", "bilibili", "youtube"])

    workflow_row = next(row for row in rows if row["category_id"] == "personal_workflow")
    comparison_row = next(row for row in rows if row["category_id"] == "tool_comparison")

    assert workflow_row["counts"] == {"reddit": 2, "bilibili": 1, "youtube": 0}
    assert workflow_row["total"] == 3
    assert comparison_row["counts"] == {"reddit": 0, "bilibili": 0, "youtube": 1}
    assert comparison_row["total"] == 1


def test_group_items_by_tier_and_category_sorts_scores_desc():
    items = [
        _item("reddit", "prompt_agent", 4.8),
        _item("reddit", "personal_workflow", 5.2),
        _item("reddit", "personal_workflow", 6.1),
    ]

    grouped = _group_items_by_tier_and_category(items)

    tier1_workflow = grouped[1]["personal_workflow"]
    tier2_prompt = grouped[2]["prompt_agent"]

    assert [item["score_total"] for item in tier1_workflow] == [6.1, 5.2]
    assert [item["score_total"] for item in tier2_prompt] == [4.8]


def test_extract_display_content_prefers_summary_json_and_cleans_text():
    item = {
        "title": "Arthur Engine 🚀",
        "summary": json.dumps(
            {
                "title_zh": "Arthur Engine：AI/ML监控治理平台™",
                "summary": "支持企业级 AI 治理〜并清理零宽字符\u200b。",
            },
            ensure_ascii=False,
        ),
    }

    title, summary = _extract_display_content(item)

    assert "🚀" not in title
    assert "™" not in title
    assert "TM" in title
    assert "〜" not in summary
    assert "\u200b" not in summary


def test_clean_pdf_text_normalizes_problematic_glyphs():
    text = "OpenClaw\u200b 🚀 ™ É 〜"

    cleaned = _clean_pdf_text(text)

    assert "\u200b" not in cleaned
    assert "🚀" not in cleaned
    assert "™" not in cleaned
    assert "É" not in cleaned
    assert "〜" not in cleaned
    assert "TM" in cleaned
    assert "E" in cleaned
    assert "~" in cleaned
