# src/processor/filter.py
import json
import logging
from src.collectors.base import RawItem
from src.prompts.filter import FILTER_PROMPT, CATEGORY_IDS

logger = logging.getLogger(__name__)

# LLM quality dimension keys returned by FILTER_PROMPT
LLM_SCORE_KEYS = [
    "practice_depth", "reproducibility", "info_density",
    "originality", "problem_solution_arc",
]

FAST_FILTER_PROMPT = """You are an AI content filter for an LLM/AI engineering practice community.

Core focus: **AI 实践** — someone using AI to solve real problems, sharing hands-on experience.

PASS if ANY of these apply:
- Has code, tutorials, step-by-step guides for AI/LLM tools
- Real experience report using AI tools (lessons, gotchas, workflows)
- Benchmarks, comparisons with actual data
- Production deployment or architecture decisions
- New open-source AI tool/framework with usage examples
- GitHub repo that is an AI tool, library, or framework

SOURCE-SPECIFIC CRITERIA:
- **github_trending**: PASS if it's an AI/ML tool, library, model, or framework. REJECT if unrelated to AI.
- **reddit/hackernews**: PASS if the discussion contains practical AI experience. REJECT pure news or opinions.
- **twitter**: PASS if the tweet shares a concrete AI insight, tool tip, or links to useful content.
- **arxiv/hf_papers**: PASS if the paper has practical applications. REJECT pure theory with no engineering relevance.
- **youtube/bilibili**: PASS if the video is a tutorial, experience share, or tool demo. REJECT reaction videos or pure news.
- **rss_blogs**: PASS if the blog post has technical depth about AI practice.

REJECT if:
- Pure company news/announcements without technical content
- Marketing, course promotion, "零基础入门"
- Opinion without hands-on experience
- Not related to AI/LLM at all

For each item, respond with JSON:
{{"results": [
  {{"index": 0, "decision": "pass" or "reject", "reason": "brief reason"}}
]}}

Items to evaluate:
"""

DEEP_FILTER_PROMPT = """You are evaluating whether this article contains genuine LLM/AI engineering practice content.

Read the full content below and determine:
1. Does it contain actual code, commands, or configurations?
2. Does it describe real-world usage experience?
3. Is the content reproducible by a reader?
4. Does it go beyond surface-level announcements?

Respond with JSON:
{{"decision": "pass" or "reject", "reason": "detailed reason"}}

Title: {title}
Source: {source}
Content:
{content}
"""


class AIFilter:
    def __init__(self, llm_client, batch_size: int = 10):
        self.llm = llm_client
        self.batch_size = batch_size

    async def fast_filter(self, items: list[RawItem]) -> tuple[list[RawItem], list[tuple[RawItem, str]]]:
        """Fast filter using cheap model. Returns (passed, rejected_with_reasons)."""
        passed = []
        rejected = []

        for i in range(0, len(items), self.batch_size):
            batch = items[i:i + self.batch_size]
            batch_text = "\n".join(
                f"[{j}] Source: {item.source} | Title: {item.title} | "
                f"Author: {item.author} | "
                f"Content: {item.content[:300]}"
                for j, item in enumerate(batch)
            )
            prompt = FAST_FILTER_PROMPT + batch_text

            try:
                response = await self.llm.complete(prompt, model_tier="fast")
                results = self._parse_filter_response(response)
                for j, item in enumerate(batch):
                    result = results.get(j, {"decision": "pass", "reason": "no response"})
                    if result["decision"] == "pass":
                        passed.append(item)
                    else:
                        rejected.append((item, result.get("reason", "rejected by fast filter")))
            except Exception as e:
                logger.error("Fast filter failed: %s", e)
                # On failure, pass all items through
                passed.extend(batch)

        return passed, rejected

    async def full_filter(self, items: list[RawItem], concurrency: int = 5) -> tuple[
        list[tuple[RawItem, str, dict]],   # passed: (item, category, llm_scores)
        list[tuple[RawItem, str]],          # rejected: (item, reason)
    ]:
        """Filter + categorize + LLM-score using FILTER_PROMPT.

        Processes batches concurrently (up to `concurrency` parallel LLM calls).
        Returns (passed, rejected) where passed items include category and
        5 LLM quality dimension scores.
        """
        import asyncio

        batches = []
        for i in range(0, len(items), self.batch_size):
            batches.append(items[i:i + self.batch_size])

        total_batches = len(batches)
        sem = asyncio.Semaphore(concurrency)
        completed = [0]

        async def process_batch(batch_idx: int, batch: list[RawItem]):
            async with sem:
                items_text = "\n".join(
                    f"[{j}] 标题: {item.title}\n"
                    f"    来源: {item.source} | 作者: {item.author} | "
                    f"互动: {self._fmt_engagement(item)}\n"
                    f"    简介: {(item.content or '')[:400]}"
                    for j, item in enumerate(batch)
                )
                prompt = FILTER_PROMPT.format(items=items_text)

                batch_passed = []
                batch_rejected = []
                try:
                    response = await self.llm.complete(prompt, model_tier="fast")
                    results = self._parse_filter_response(response)
                    for j, item in enumerate(batch):
                        result = results.get(j, {"decision": "reject", "reason": "no response"})
                        if result.get("decision") == "pass":
                            category = result.get("category", "personal_exp")
                            if category not in CATEGORY_IDS:
                                category = "personal_exp"
                            llm_scores = {}
                            raw_scores = result.get("scores", {})
                            for key in LLM_SCORE_KEYS:
                                val = raw_scores.get(key, 0.0)
                                try:
                                    llm_scores[key] = max(0.0, min(1.0, float(val)))
                                except (ValueError, TypeError):
                                    llm_scores[key] = 0.0
                            batch_passed.append((item, category, llm_scores))
                        else:
                            batch_rejected.append((item, result.get("reason", "rejected")))
                except Exception as e:
                    logger.error("full_filter batch %d failed: %s", batch_idx + 1, e)
                    for item in batch:
                        batch_passed.append((item, "personal_exp", {k: 0.5 for k in LLM_SCORE_KEYS}))
                finally:
                    completed[0] += 1
                    if completed[0] % 5 == 0 or completed[0] == total_batches:
                        logger.info("full_filter progress: %d/%d batches", completed[0], total_batches)

                return batch_passed, batch_rejected

        tasks = [process_batch(i, batch) for i, batch in enumerate(batches)]
        results = await asyncio.gather(*tasks)

        passed = []
        rejected = []
        for bp, br in results:
            passed.extend(bp)
            rejected.extend(br)

        logger.info("full_filter done: %d passed, %d rejected", len(passed), len(rejected))
        return passed, rejected

    @staticmethod
    def _fmt_engagement(item: RawItem) -> str:
        """Format engagement metrics for the prompt."""
        m = item.metadata
        parts = []
        for key in ("views", "play", "view_count"):
            v = m.get(key)
            if v:
                parts.append(f"播放 {v}")
                break
        for key in ("likes", "like_count", "score", "points", "stars"):
            v = m.get(key)
            if v:
                parts.append(f"{key} {v}")
                break
        for key in ("num_comments", "comments", "comment_count"):
            v = m.get(key)
            if v:
                parts.append(f"评论 {v}")
                break
        dur = m.get("duration")
        if dur:
            parts.append(f"时长 {dur}s")
        return " | ".join(parts) if parts else "无数据"

    async def deep_filter(self, items: list[RawItem], extractor=None) -> tuple[list[RawItem], list[tuple[RawItem, str]]]:
        """Deep filter with content extraction. Returns (passed, rejected_with_reasons)."""
        passed = []
        rejected = []

        for item in items:
            content = item.content
            if extractor and item.url:
                extracted = await extractor.extract(item.url)
                if extracted:
                    content = extracted[:3000]

            prompt = DEEP_FILTER_PROMPT.format(
                title=item.title, source=item.source,
                content=content[:3000] if content else "No content available"
            )

            try:
                response = await self.llm.complete(prompt, model_tier="standard")
                result = self._parse_single_response(response)
                if result.get("decision") == "pass":
                    passed.append(item)
                else:
                    rejected.append((item, result.get("reason", "rejected by deep filter")))
            except Exception as e:
                logger.error("Deep filter failed for %s: %s", item.url, e)
                passed.append(item)  # On failure, pass through

        return passed, rejected

    def _parse_filter_response(self, response: str) -> dict:
        try:
            # Try to extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                return {r["index"]: r for r in data.get("results", [])}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse filter response: %s", e)
        return {}

    def _parse_single_response(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse filter response: %s", e)
        return {"decision": "pass", "reason": "parse failure, defaulting to pass"}
