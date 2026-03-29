# src/processor/summarizer.py
import json
import logging
import re
from datetime import datetime, timezone

from deep_translator import GoogleTranslator
from src.collectors.base import RawItem

logger = logging.getLogger(__name__)

_translator = GoogleTranslator(source="auto", target="zh-CN")


def _is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", (text or "")[:100]))


def _translate(text: str, max_len: int = 4500) -> str:
    """Translate to Chinese via Google Translate. Skips if already Chinese."""
    if not text or _is_chinese(text):
        return text
    try:
        return _translator.translate(text[:max_len]) or text
    except Exception as e:
        logger.debug("translate failed: %s", e)
        return text

ITEM_SUMMARY_PROMPT = """你是 AI 工程实践社区的中文内容总结员。必须用中文回复。

标题: {title}
来源: {source}
作者: {author}
内容: {content}

必须用以下 JSON 格式回复（不要 markdown 代码块，必须是中文）：
{{"title_zh": "中文标题（如果原标题是中文则保留原文，英文标题翻译成中文）", "summary": "2-3句中文摘要，重点说：做了什么、关键发现、可借鉴的经验"}}"""

BATCH_SUMMARY_PROMPT = """你是 AI 工程实践社区的中文内容总结员。为以下每条内容生成中文标题和摘要。

{items}

必须返回纯 JSON（不要 markdown 代码块，必须全部中文）：
{{"results": [
  {{"index": 0, "title_zh": "中文标题", "summary": "2-3句中文摘要，重点说做了什么、关键发现、可借鉴经验"}}
]}}"""

DIGEST_PROMPT = """Create a daily digest for an AI/LLM engineering practice community.
Group the items by quality tier and create a readable newsletter format.

Items (sorted by score, highest first):
{items_text}

Format the digest as markdown with:
1. A brief header with date and item count
2. "Highlights" section for top-scored items (score >= 3.5)
3. "Worth Reading" section for remaining items
4. A brief stats line at the end

Keep each item entry to 2-3 lines max. Include the source, score, and link."""


class Summarizer:
    def __init__(self, llm_client):
        self.llm = llm_client

    async def summarize_item(self, item: RawItem, score=None) -> dict:
        """Returns {"title_zh": "...", "summary": "..."} with Chinese translation."""
        prompt = ITEM_SUMMARY_PROMPT.format(
            title=item.title, source=item.source,
            author=item.author, content=item.content[:2000]
        )
        try:
            response = await self.llm.complete(prompt, model_tier="fast")
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(response[start:end])
                title_zh = result.get("title_zh", item.title)
                summary = result.get("summary", "")
            else:
                title_zh = item.title
                summary = response[:300] if response else item.content[:200]
        except Exception as e:
            logger.warning("Summarization failed for %s: %s", item.title, e)
            title_zh = item.title
            summary = item.content[:200]

        # Ensure Chinese via Google Translate fallback
        title_zh = _translate(title_zh)
        summary = _translate(summary)
        return {"title_zh": title_zh, "summary": summary}

    async def batch_summarize(self, items: list[RawItem],
                              batch_size: int = 10, concurrency: int = 5) -> list[dict]:
        """Batch summarize items concurrently. Returns list of {"title_zh": ..., "summary": ...}."""
        import asyncio

        results = [{"title_zh": item.title, "summary": ""} for item in items]
        sem = asyncio.Semaphore(concurrency)
        completed = [0]
        total_batches = (len(items) + batch_size - 1) // batch_size

        async def process_batch(batch_start: int):
            async with sem:
                batch = items[batch_start:batch_start + batch_size]
                items_text = "\n".join(
                    f"[{j}] 标题: {item.title}\n"
                    f"    来源: {item.source} | 作者: {item.author}\n"
                    f"    内容: {(item.content or '')[:300]}"
                    for j, item in enumerate(batch)
                )
                prompt = BATCH_SUMMARY_PROMPT.format(items=items_text)

                try:
                    response = await self.llm.complete(prompt, model_tier="fast")
                    start = response.find("{")
                    end = response.rfind("}") + 1
                    if start >= 0 and end > start:
                        data = json.loads(response[start:end])
                        for r in data.get("results", []):
                            idx = r.get("index", -1)
                            if 0 <= idx < len(batch):
                                results[batch_start + idx] = {
                                    "title_zh": r.get("title_zh", batch[idx].title),
                                    "summary": r.get("summary", ""),
                                }
                except Exception as e:
                    logger.warning("Batch summarize failed: %s", e)
                finally:
                    completed[0] += 1
                    if completed[0] % 5 == 0 or completed[0] == total_batches:
                        logger.info("batch_summarize progress: %d/%d", completed[0], total_batches)

        tasks = [process_batch(i) for i in range(0, len(items), batch_size)]
        await asyncio.gather(*tasks)

        # Ensure all results are Chinese via Google Translate fallback
        for r in results:
            r["title_zh"] = _translate(r["title_zh"])
            r["summary"] = _translate(r["summary"])

        return results

    async def generate_daily_digest(self, items: list[dict]) -> str:
        items_text = "\n".join(
            f"- [{i+1}] {item.get('title', '')} (source: {item.get('source', '')}, "
            f"score: {item.get('score_total', 0):.1f}, url: {item.get('url', '')})\n"
            f"  Summary: {item.get('summary', 'No summary')[:200]}"
            for i, item in enumerate(items)
        )
        prompt = DIGEST_PROMPT.format(items_text=items_text)
        try:
            return await self.llm.complete(prompt, model_tier="standard")
        except Exception as e:
            logger.warning("Digest generation failed: %s", e)
            return self._fallback_digest(items)

    async def generate_compact_digest(self, items: list[dict], max_bytes: int = 4096) -> str:
        """Generate compact digest for WeChat (4096 byte limit)."""
        lines = [f"AI实践日报 ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n"]
        lines.append(f"共 {len(items)} 条精选\n")

        for i, item in enumerate(items[:5]):
            title = item.get("title", "")[:40]
            source = item.get("source", "")
            score = item.get("score_total", 0)
            lines.append(f"\n{i+1}. [{source}] {title}")
            lines.append(f"   评分: {score:.1f}/5.0")

        lines.append("\n\n详细内容请查看邮件")

        result = "\n".join(lines)
        if len(result.encode("utf-8")) > max_bytes:
            result = result[:max_bytes // 3]  # rough CJK-safe truncation
        return result

    def _fallback_digest(self, items: list[dict]) -> str:
        lines = [f"# AI实践日报 ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n"]
        for i, item in enumerate(items):
            lines.append(f"{i+1}. **{item.get('title', '')}**")
            lines.append(f"   Source: {item.get('source', '')} | Score: {item.get('score_total', 0):.1f}")
            lines.append(f"   {item.get('url', '')}\n")
        return "\n".join(lines)
