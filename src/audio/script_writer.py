"""Generate podcast-style script from weekly report items using LLM."""

from __future__ import annotations

import json
import logging
from src.prompts.filter import TIER_MAP
from src.report.i18n import get_category_name

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_ZH = """\
你的角色是 AI 工程技术简报播报员。不是播客主持人，不讲故事，不煽情，不引导听众情绪。
目标听众是 AI 工程师、研究者、工具链作者——他们要的是信息，不是陪伴。
输出是一份技术简报的朗读稿，靠密度和准确度取胜。

【不要用讲故事的方式，要用技术内容陈述的方式】
对比：
  ✗ 讲故事写法：
    "这个故事非常技术，但也非常经典。一位开发者把自己用 Go 写的自适应对冲请求库，
     名字叫 hedge，拿去做测试…结果发现，这个库完全失效了。核心原因很有意思，
     也暴露了传统网络编程思维与新兴 AI 服务模式之间的摩擦…"
  ✓ 技术陈述写法：
    "hedge 是一个 Go 自适应对冲请求库，对接流式 LLM 推理服务时失效。原因：
     LLM 推理服务器为降低首包延迟，会在收到请求后立即返回 200 OK，再流式推送
     token。hedge 把 200 OK 当作请求成功信号，取消其他对冲请求，但真正的内容
     交付才刚开始。结论：为 AI 流式服务设计的客户端库，不能以 HTTP 状态码
     作为完成信号，应以 EOF 或最后一个 token 为准。"

技术陈述的基本写法：
- 直接说它是什么、用什么技术栈、关键参数/版本号是多少
- 陈述事实，不评论它"有意思"、"经典"、"令人震惊"
- 引用具体数字：token 数、延迟毫秒数、金额、百分比、版本号、模型名、参数名
- 结论一句话给出技术性的要点（可复用的做法、失效的场景、配置值）
- 不要替听众总结"这意味着什么"，工程师自己会判断

【输出格式 · TTS 朗读，极其重要】
此稿会直接送给 TTS 朗读，任何非口语化符号会被原样念出。
- 禁止所有 markdown：**加粗** *斜体* # 标题 `代码` [链接](url) 列表的 - 或 *
- 禁止舞台指示和标签："（开场音乐）"、"（停顿）"、"主持人："
- 禁止独立成行的章节标题（"本周亮点"、"Tier 1 深度播报"）；用过渡句串联
- 禁止 emoji、特殊符号
- 不要"第一条"、"第二条"，用"先看"、"下一条"、"再一条" 等衔接
- 段落间空行分隔，段内连贯行文
- 术语、模型名、版本号用中英混排自然读法即可（如 Claude Opus 4.7、GPT-4o、cache TTL）

【禁用的废话模式】
- 故事化套话："这是一个 X 的故事"、"听起来很科幻"、"非常戏剧性"、"有意思的是"、"令人震惊的是"、"精彩之处在于"
- 代入式描述："想象一下"、"你可以把自己想成那个开发者"、"就像在看一部剧"
- 升华总结："这不仅仅是 X，更是 Y"、"这背后是…"、"这标志着一个时代"、"这释放了一个信号"
- 主观价值评判："令人深思"、"值得深究"、"引人注目"、"耐人寻味"
- 开场寒暄："嘿，大家好"、"欢迎收听"、"又到了…的时间"、"我是你的主播"
- 听众互动："准备好了吗？"、"话不多说"、"我们一起来看看"
- 元叙述："在深入细节之前我们先…"、"接下来我想聊聊…"、"话说回来"
- 结构宣告："首先…其次…最后…"、"以上就是本周亮点"
- 陈词滥调："干货满满"、"划重点"、"不容错过"
- 结尾客套："感谢收听"、"我们下期再见"

原则：每一句话都必须承载具体技术信息——事实、数据、参数、结论。如果一句去掉后不丢失任何技术信息，它就是废话，删掉。

【开场 · 正确写法】
直接报期号和本周最关键的技术事件。例：
"本期 AI 工程周报，覆盖 4 月 12 日到 19 日，共 100 条。核心事件：Claude Opus 4.7
发布后，社区实测发现 token 消耗上升约 30%，cache TTL 从 1 小时被静默下调到 5 分钟；
AMD 工程师基于 6971 条 Claude Code 会话的分析数据被广泛引用。"

【结尾 · 正确写法】
一句话。例："本期完。详细条目与源链接见 PDF。"

【每条的技术陈述模板】
Tier 1（每条 3-5 段）：
  段 1：项目/工具/论文名 + 来源 + 核心事实（做了什么、解决什么）
  段 2：关键技术参数（版本、配置、数据量、性能指标）
  段 3：失效场景 / 踩坑点 / 条件约束（具体而不是抽象）
  段 4：对比或前后差异（数字对比）
  段 5：可复用的做法 / 结论（一句话给出工程师能抄走的东西）

Tier 2（每条 1-3 段）：
  项目名 + 来源 + 核心事实 + 关键参数/结果 + 一句结论。

Tier 3 / 花边（每条 1 句）：
  项目名 + 来源 + 一句事实陈述（不点评）。

【长度】目标 10000-12000 中文字符。硬性要求：不得低于 9000 字。靠信息密度撑够，不靠抒情和重复。"""

SYSTEM_PROMPT_EN = """\
You are a professional AI tech podcast host. Your tone is conversational and insightful, \
like chatting with a knowledgeable colleague.
Your task is to generate a ~20-25 minute podcast script from this week's curated AI practice content.

[OUTPUT FORMAT — CRITICAL]
This script is fed directly into a TTS engine. Anything non-speakable will be read aloud literally and sound stupid.
- Absolutely NO markdown: no **bold**, *italics*, # headers, `code`, [links](url), or bullet markers (-, *).
- Absolutely NO stage directions: no "(intro music, fading)", "(pause)", "(SFX)", no speaker labels like "Host:".
- Absolutely NO section heading lines like "Weekly Highlights" or "Tier 1 Deep Dive" as standalone titles — use conversational transitions instead (e.g. "Let's start with the stuff worth digging into…").
- Absolutely NO emojis, special symbols, or numbered lists ("1.", "2.").
- Do not say "first item", "second item" — use natural connectors like "kicking things off", "next up", "another one worth flagging", "speaking of which".
- Separate paragraphs with a blank line; keep prose flowing inside a paragraph — no bullet points.

[CONTENT REQUIREMENTS]
- Length: target 6000-8000 English words. Hard minimum: 5500 words.
- Conversational but dense — every sentence carries information, no padding.

[BANNED FILLER PATTERNS — do NOT produce any of these]
- Podcast greetings: "Hey everyone, welcome back", "It's your host", "I'm coming to you", "What's up folks"
- Audience hooks: "Are you ready?", "Let's dive in", "Without further ado", "Buckle up", "Stay with me"
- Meta-narration (announcing what you're about to say): "Before we get into the details, let's zoom out", "Our goal isn't just X, it's Y", "Let's take a step back"
- Faux-profound transitions: "This isn't just news, it's a signal", "What's really going on here is…", "This is a watershed moment" — unless you have concrete evidence
- Structure announcements: "First… second… finally…", "That wraps up the highlights section"
- Buzzwords: "game-changer", "paradigm shift", "must-read", "don't sleep on this", "hot take"
- Performative qualifiers: "honestly", "to be real with you", "as a practitioner I'd say"
- Outro pleasantries: "Thanks for listening", "Catch you next week", "Hit subscribe"

[OPENING — correct example]
First sentence is already information. Example: "The week of April 19th, 2026 — one hundred items, and the dominant thread is Claude Opus 4.7's performance and cost backlash: an AMD engineer analyzed almost seven thousand Claude Code sessions, users proved cache TTL was silently dropped from one hour to five minutes…"
No warmup.

[CLOSING — correct example]
One sentence. "That's it for this week — details are in the PDF."

[HIGHLIGHTS BLOCK]
Real trends with concrete evidence (which item, what data). No reciting titles. Each trend anchored to specific pieces.

Principle: if you could delete a sentence and lose no information, it's filler — cut it.

[COVERAGE DEPTH]
- Tier 1 items: spoken title + source + 3-5 paragraphs (why it matters, key findings, lessons)
- Tier 2 items: spoken title + source + 1-3 paragraphs (core highlight, why noteworthy)
- Tier 3 and novelty: spoken title + source + one-sentence take
- Never just list titles. Every item gets commentary."""


def _build_user_prompt(items: list[dict], week_start: str, week_end: str, lang: str) -> str:
    """Build structured data prompt for the LLM."""
    tier_items: dict[int, list[dict]] = {1: [], 2: [], 3: [], 0: []}
    for item in items:
        tags = item.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        category = tags[0] if isinstance(tags, list) and tags else "personal_exp"
        tier = TIER_MAP.get(category, 3)

        summary_data = item.get("summary", "")
        if isinstance(summary_data, str):
            try:
                summary_data = json.loads(summary_data)
            except (json.JSONDecodeError, TypeError):
                summary_data = {}

        if lang == "zh":
            title = (summary_data.get("title_zh") or item.get("title", "")) if isinstance(summary_data, dict) else item.get("title", "")
            summary = summary_data.get("summary", "") if isinstance(summary_data, dict) else ""
        else:
            title = item.get("title", "")
            summary = (item.get("content", "") or "")[:400]

        source = item.get("source", "")
        score = item.get("score_total", 0)
        cat_name = get_category_name(category, lang)

        tier_items[tier].append({
            "title": title,
            "source": source,
            "category": cat_name,
            "score": round(float(score), 1) if score else 0,
            "summary": summary[:300],
        })

    # Sort each tier by score descending
    for tier in tier_items:
        tier_items[tier].sort(key=lambda x: x["score"], reverse=True)

    if lang == "zh":
        prompt = f"日期范围：{week_start} ~ {week_end}\n"
        prompt += f"总条目数：{len(items)}\n\n"
        prompt += "⚠️ 脚本目标长度：15000-20000 字（约 20-25 分钟），请务必写够！\n\n"
        prompt += "请生成播客脚本，结构如下：\n"
        prompt += "1. 开场白（简短欢迎 + 日期 + 条目数，1 分钟）\n"
        prompt += "2. 本周亮点总结（从 Tier 1 内容中提炼 3-5 个关键趋势，3-4 分钟）\n"
        prompt += "3. Tier 1 逐条深度播报（每条：标题 + 来源 + 3-5 段详细解说，讲清楚为什么重要、关键发现、可借鉴经验，10-12 分钟）\n"
        prompt += "4. Tier 2 逐条简要播报（每条：标题 + 来源 + 1-3 段简要解说，核心亮点和关注原因，5-7 分钟）\n"
        prompt += "5. Tier 3 + 花边速览（每条：标题 + 来源 + 一句话点评，2-3 分钟）\n"
        prompt += "6. 结尾（感谢收听，完整报告见 PDF，1 分钟）\n\n"
    else:
        prompt = f"Date range: {week_start} ~ {week_end}\n"
        prompt += f"Total items: {len(items)}\n\n"
        prompt += "⚠️ Target script length: 8000-12000 words (~20-25 minutes). You MUST write the full length!\n\n"
        prompt += "Generate a podcast script with this structure:\n"
        prompt += "1. Opening (brief welcome + date + item count, 1 min)\n"
        prompt += "2. Weekly highlights (synthesize 3-5 key trends from Tier 1, 3-4 min)\n"
        prompt += "3. Tier 1 detailed coverage (each: title + source + 3-5 paragraphs explaining why it matters, key findings, lessons, 10-12 min)\n"
        prompt += "4. Tier 2 brief coverage (each: title + source + 1-3 paragraphs on core highlight and why it's noteworthy, 5-7 min)\n"
        prompt += "5. Tier 3 + novelty quick scan (each: title + source + one-sentence commentary, 2-3 min)\n"
        prompt += "6. Closing (thanks, full report in PDF, 1 min)\n\n"

    tier_labels = {
        "zh": {1: "Tier 1 · 实践经验", 2: "Tier 2 · 技术深度", 3: "Tier 3 · 宏观视野", 0: "花边/创意"},
        "en": {1: "Tier 1 · Practice", 2: "Tier 2 · Deep Tech", 3: "Tier 3 · Big Picture", 0: "Novelty"},
    }

    for tier in (1, 2, 3, 0):
        items_list = tier_items[tier]
        if not items_list:
            continue
        label = tier_labels[lang][tier]
        prompt += f"=== {label} ({len(items_list)} items) ===\n"
        for entry in items_list:
            prompt += f"- [{entry['source']}] {entry['title']} (score: {entry['score']}, category: {entry['category']})\n"
            if entry['summary']:
                prompt += f"  Summary: {entry['summary']}\n"
        prompt += "\n"

    return prompt


def _fallback_script(items: list[dict], week_start: str, week_end: str, lang: str) -> str:
    """Template-based fallback when LLM fails."""
    tier_items: dict[int, list[dict]] = {1: [], 2: [], 3: [], 0: []}
    for item in items:
        tags = item.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        category = tags[0] if isinstance(tags, list) and tags else "personal_exp"
        tier = TIER_MAP.get(category, 3)

        summary_data = item.get("summary", "")
        if isinstance(summary_data, str):
            try:
                summary_data = json.loads(summary_data)
            except (json.JSONDecodeError, TypeError):
                summary_data = {}

        if lang == "zh":
            title = (summary_data.get("title_zh") or item.get("title", "")) if isinstance(summary_data, dict) else item.get("title", "")
            summary = summary_data.get("summary", "") if isinstance(summary_data, dict) else ""
        else:
            title = item.get("title", "")
            summary = (item.get("content", "") or "")[:200]

        source = item.get("source", "")
        tier_items[tier].append({"title": title, "source": source, "summary": summary})

    lines = []
    if lang == "zh":
        lines.append(f"欢迎收听 AI 实践周报，本期覆盖 {week_start} 到 {week_end}，共精选 {len(items)} 条内容。\n")
        for entry in tier_items[1]:
            lines.append(f"来自 {entry['source']}，{entry['title']}。{entry['summary']}\n")
        lines.append("接下来快速过一下其他值得关注的内容。\n")
        for tier in (2, 3, 0):
            for entry in tier_items[tier]:
                lines.append(f"{entry['source']}，{entry['title']}。\n")
        lines.append("感谢收听，完整周报请查看 PDF 版本。\n")
    else:
        lines.append(f"Welcome to AI Practice Weekly, covering {week_start} to {week_end}, with {len(items)} curated items.\n")
        for entry in tier_items[1]:
            lines.append(f"From {entry['source']}, {entry['title']}. {entry['summary']}\n")
        lines.append("Here are more items worth noting this week.\n")
        for tier in (2, 3, 0):
            for entry in tier_items[tier]:
                lines.append(f"{entry['source']}, {entry['title']}.\n")
        lines.append("Thanks for listening. Check the PDF for the full report.\n")

    return "\n".join(lines)


class ScriptWriter:
    def __init__(self, llm_client, config: dict):
        self.llm = llm_client
        self.config = config

    async def generate(
        self,
        items: list[dict],
        week_start: str,
        week_end: str,
        lang: str = "zh",
    ) -> str:
        """Generate podcast script. Falls back to template on LLM failure."""
        if not items:
            return ""

        system = SYSTEM_PROMPT_ZH if lang == "zh" else SYSTEM_PROMPT_EN
        user_prompt = _build_user_prompt(items, week_start, week_end, lang)
        full_prompt = f"{system}\n\n{user_prompt}"

        try:
            script = await self.llm.complete(full_prompt, model_tier="standard", max_tokens=8192)
            logger.info("Script generated for %s (%d chars, %s)", f"{week_start}~{week_end}", len(script), lang)
            return script
        except Exception as e:
            logger.warning("LLM script generation failed (%s), using fallback template: %s", lang, e)
            return _fallback_script(items, week_start, week_end, lang)
