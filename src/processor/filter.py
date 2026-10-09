# src/processor/filter.py
import json
import logging
import re
from src.collectors.base import RawItem
from src.prompts.filter import FILTER_PROMPT, CATEGORY_IDS


def _fix_unescaped_inner_quotes(s: str) -> str:
    """Rewrite ASCII double-quotes that appear *inside* a JSON string
    value, replacing them with the fullwidth Unicode `"` so the JSON
    parser does not see them as string terminators. We detect a value
    boundary by looking ahead for the next legal terminator: a `"`
    followed by zero-or-more whitespace, then `,`, `}`, or `]`.

    This is a best-effort rescue for LLM responses that include quoted
    titles inside `reason` strings without escaping the inner quotes.
    """
    out = []
    i = 0
    n = len(s)
    in_string = False
    while i < n:
        c = s[i]
        if not in_string:
            out.append(c)
            if c == '"':
                in_string = True
            i += 1
            continue
        # in_string == True
        if c == '\\' and i + 1 < n:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == '"':
            # Is this a real terminator? Look ahead past whitespace.
            j = i + 1
            while j < n and s[j] in ' \t\r\n':
                j += 1
            if j < n and s[j] in ',}]:':
                # Real terminator (`:` for keys, `,}]` for values)
                out.append('"')
                in_string = False
                i += 1
                continue
            # Not a terminator — must be an unescaped inner quote.
            # Replace with fullwidth and keep going inside the string.
            out.append('”')   # right double quotation mark
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)

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

    async def full_filter(self, items: list[RawItem], concurrency: int = 5,
                          on_batch_done=None) -> tuple[
        list[tuple[RawItem, str, dict]],   # passed: (item, category, llm_scores)
        list[tuple[RawItem, str]],          # rejected: (item, reason)
    ]:
        """Filter + categorize + LLM-score using FILTER_PROMPT.

        Processes batches concurrently (up to `concurrency` parallel LLM calls).
        Returns (passed, rejected) where passed items include category and
        5 LLM quality dimension scores.

        If ``on_batch_done`` is provided, it is awaited after each batch
        finishes with signature ``(batch_idx, batch_passed, batch_rejected)``.
        Use this to persist results incrementally so an interrupted run
        only loses in-flight batches rather than the whole result set.
        """
        import asyncio

        batches = []
        for i in range(0, len(items), self.batch_size):
            batches.append(items[i:i + self.batch_size])

        total_batches = len(batches)
        sem = asyncio.Semaphore(concurrency)
        completed = [0]

        max_attempts = 3

        async def call_llm_with_retry(prompt: str, batch_idx: int) -> dict:
            """Call LLM and parse. Retries up to max_attempts times on
            empty parse or transport error. Returns {} only after all
            retries are exhausted."""
            last_err: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await self.llm.complete(
                        prompt, model_tier="fast", max_tokens=8192,
                    )
                    results = self._parse_filter_response(response)
                    if results:
                        if attempt > 1:
                            logger.info(
                                "full_filter batch %d: parsed on attempt %d",
                                batch_idx + 1, attempt,
                            )
                        return results
                    # parse returned {} — treat as transient and retry
                    last_err = RuntimeError("empty parse result")
                    logger.warning(
                        "full_filter batch %d: parse empty (attempt %d/%d), retrying",
                        batch_idx + 1, attempt, max_attempts,
                    )
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "full_filter batch %d: LLM call error (attempt %d/%d): %s",
                        batch_idx + 1, attempt, max_attempts, e,
                    )
                if attempt < max_attempts:
                    backoff = 2.0 * (2 ** (attempt - 1))   # 2s, 4s, 8s
                    await asyncio.sleep(backoff)
            logger.error(
                "full_filter batch %d: all %d attempts failed (last: %s)",
                batch_idx + 1, max_attempts, last_err,
            )
            return {}

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
                results = await call_llm_with_retry(prompt, batch_idx)

                if not results:
                    # All retries failed: pass items through with neutral
                    # scores rather than silently rejecting them. (User
                    # would rather see a possibly-borderline item than
                    # lose a good one to a parser bug.)
                    logger.warning(
                        "full_filter batch %d: passing %d items through "
                        "with neutral scores after all retries failed",
                        batch_idx + 1, len(batch),
                    )
                    for item in batch:
                        batch_passed.append(
                            (item, "personal_exp",
                             {k: 0.5 for k in LLM_SCORE_KEYS}))
                else:
                    for j, item in enumerate(batch):
                        result = results.get(j)
                        if result is None:
                            # Parser returned dict but missing this index —
                            # also pass through with neutral scores
                            batch_passed.append(
                                (item, "personal_exp",
                                 {k: 0.5 for k in LLM_SCORE_KEYS}))
                            continue
                        if result.get("decision") == "pass":
                            category = result.get("category", "personal_exp")
                            if category not in CATEGORY_IDS:
                                category = "personal_exp"
                            llm_scores = {}
                            raw_scores = result.get("scores") or {}
                            for key in LLM_SCORE_KEYS:
                                val = raw_scores.get(key, 0.0)
                                try:
                                    llm_scores[key] = max(0.0, min(1.0, float(val)))
                                except (ValueError, TypeError):
                                    llm_scores[key] = 0.0
                            batch_passed.append((item, category, llm_scores))
                        else:
                            batch_rejected.append((item, result.get("reason", "rejected")))

                completed[0] += 1
                if completed[0] % 5 == 0 or completed[0] == total_batches:
                    logger.info("full_filter progress: %d/%d batches", completed[0], total_batches)

                if on_batch_done is not None:
                    try:
                        await on_batch_done(batch_idx, batch_passed, batch_rejected)
                    except Exception as e:
                        logger.error(
                            "on_batch_done callback raised for batch %d: %s",
                            batch_idx + 1, e,
                        )

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
        candidates = self._json_candidates(response)
        last_err: Exception | None = None
        for cand in candidates:
            try:
                data = json.loads(cand)
                return {r["index"]: r for r in data.get("results", [])}
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                last_err = e
                continue
        # All recovery attempts failed — log a diagnostic snippet (head + tail)
        snippet = self._snippet_for_log(response)
        logger.warning("Failed to parse filter response: %s | snippet=%s",
                       last_err, snippet)
        self._dump_failed_response(response, last_err)
        return {}

    @staticmethod
    def _json_candidates(response: str) -> list[str]:
        """Yield progressively-more-tolerant JSON-string candidates from
        an LLM response. Each item is independently attempted by the
        caller — the first one that parses wins."""
        candidates: list[str] = []
        if not response:
            return candidates
        start = response.find("{")
        end = response.rfind("}") + 1
        if start < 0 or end <= start:
            return candidates
        raw = response[start:end]
        candidates.append(raw)

        # Strip trailing commas before } or ]
        no_trailing = re.sub(r",(\s*[}\]])", r"\1", raw)
        if no_trailing != raw:
            candidates.append(no_trailing)

        # Fix unescaped quotes inside string values FIRST. The LLM
        # (Claude haiku) likes to quote-cite source titles inside
        # `reason` without escaping, e.g.
        #   "reason": "标题党("最全""少走99%弯路")"
        # We walk the string left-to-right and, whenever we see a `"`
        # while inside a value, look ahead: if the next non-whitespace
        # is `,`, `}`, or `]` it's a real terminator; otherwise we
        # treat it as inner content and replace with the fullwidth `"`.
        fixed_quotes = _fix_unescaped_inner_quotes(no_trailing)
        if fixed_quotes != no_trailing:
            candidates.append(fixed_quotes)

        # Strip newlines inside string values that were not escaped
        # (DeepSeek sometimes emits literal \n inside reason strings).
        # Apply to the quote-fixed version so the string boundaries
        # are unambiguous.
        no_raw_newlines = re.sub(
            r'(?<!\\)"\s*([^"]*?)\n([^"]*?)"',
            lambda m: f'"{m.group(1)} {m.group(2)}"',
            fixed_quotes,
        )
        if no_raw_newlines != fixed_quotes:
            candidates.append(no_raw_newlines)

        # Last resort: just the `results` array if we can find it
        m = re.search(r'"results"\s*:\s*(\[.*\])', no_raw_newlines, re.DOTALL)
        if m:
            candidates.append('{"results":' + m.group(1) + "}")

        return candidates

    @staticmethod
    def _snippet_for_log(response: str, head: int = 200, tail: int = 200) -> str:
        if not response:
            return "<empty>"
        if len(response) <= head + tail + 20:
            return repr(response)
        return repr(response[:head] + " ...[truncated]... " + response[-tail:])

    @staticmethod
    def _dump_failed_response(response: str, err: Exception | None) -> None:
        """Write the failing response to a per-day diagnostic file so we
        can post-mortem the parser without needing TRACE-level logging.
        Errors here are swallowed — diagnostics must never break the run."""
        try:
            from pathlib import Path
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            out_dir = Path("logs") / "filter_parse_failures"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S_%f")
            (out_dir / f"{day}_{stamp}.txt").write_text(
                f"# Error: {err}\n# ---\n{response}",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _parse_single_response(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse filter response: %s", e)
        return {"decision": "pass", "reason": "parse failure, defaulting to pass"}
