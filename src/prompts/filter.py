# src/prompts/filter.py
"""Shared filter/categorization/scoring prompts and category definitions."""

CATEGORIES = {
    # Core: best practice for using AI
    "personal_workflow":  "AI 工具用法与工作流 — Claude Code/Cursor/Copilot 等工具实战用法、提效工作流、工具链组合",
    "prompt_agent":       "Prompt/Agent 工程 — prompt 设计、agent 编排、tool use 的实操技巧",
    "tool_comparison":    "模型/工具选型对比 — 深度实测的模型能力对比、什么场景用哪个模型/工具",
    "personal_exp":       "生产力提升经验 — 个人用 AI 提效的心得、踩坑、反直觉发现",
    # Practice: adjacent hands-on
    "team_process":       "团队协作/流程 — AI 融入团队开发流程、code review、CI/CD",
    "rag_retrieval":      "RAG/检索系统 — 向量库、检索增强生成、embedding 的工程实践",
    "postmortem":         "故障/踩坑复盘 — 失败案例、Bug 分析、事故复盘、教训总结",
    "opensource_tool":    "开源项目/工具 — 值得关注的新开源项目实测、源码解析",
    # Build: how AI is made (kept, ranked last)
    "enterprise_arch":    "企业架构 — 企业级 AI 系统设计、基础设施选型、大规模部署",
    "ai_vertical":        "AI+垂直行业 — AI 在医疗/金融/法律/教育/制造等领域的真实落地",
    "deploy_finetune":    "模型部署/微调/训练 — 从零训练、fine-tuning、量化、推理优化、本地部署实战",
    "novelty":            "花边/创意实验 — 有趣但非工程实践的创意 demo、概念验证、艺术项目",
}

CATEGORY_IDS = list(CATEGORIES.keys())

# Tiers reflect "best practice for USING AI" priority, not "how AI is
# built". Tier 1 (core) = directly helps a practitioner use AI better
# day-to-day: tool usage / workflows, model selection, prompt/agent
# engineering, productivity experience. Tier 2 (practice) = adjacent
# hands-on engineering. Tier 3 (build) = building/training/deploying AI
# systems — kept but ranked last. Tier 0 = novelty.
TIER_MAP = {
    # Tier 1: CORE — how to best use AI / which model to use
    "personal_workflow": 1,   # AI tool usage & workflows
    "prompt_agent": 1,        # prompt / agent engineering
    "tool_comparison": 1,     # model & tool selection / comparison
    "personal_exp": 1,        # productivity experience using AI
    # Tier 2: PRACTICE — adjacent hands-on engineering
    "team_process": 2,
    "rag_retrieval": 2,
    "postmortem": 2,
    "opensource_tool": 2,
    # Tier 3: BUILD — building/training/deploying AI (kept but last)
    "enterprise_arch": 3,
    "ai_vertical": 3,
    "deploy_finetune": 3,     # training / fine-tuning / quantization / serving
    # Novelty: separate section, last
    "novelty": 0,
}

# Fixed, deterministic section order for report/video rendering. Sections
# always appear in THIS order regardless of item count or score, so the
# core "using-AI" sections are always on top and build/training is always
# last. Within a section, items are still sorted by score.
CATEGORY_ORDER = [
    # — Core: best practice for using AI —
    "personal_workflow",
    "prompt_agent",
    "tool_comparison",
    "personal_exp",
    # — Practice: adjacent hands-on —
    "team_process",
    "rag_retrieval",
    "postmortem",
    "opensource_tool",
    # — Build: how AI is made (kept, ranked last) —
    "enterprise_arch",
    "ai_vertical",
    "deploy_finetune",
    # — Novelty —
    "novelty",
]

# Tier-level score multipliers — applied after weighted sum in compute_total_score
TIER_SCORE_MULTIPLIER = {
    1: 1.0,
    2: 0.92,   # adjacent practice slightly below core using-AI content
    3: 0.78,   # build/train/deploy ranks well below "how to use AI"
    0: 0.75,   # novelty ranks lowest
}

# Source-level score multipliers — applied after the weighted sum in
# compute_total_score. Bilibili is dominated by Chinese re-uploads /
# commentary of English-origin AI news (secondary content); its hype-y
# clickbait titles inflate the LLM quality dimensions while the videos
# carry modest original engineering value. Dampen the whole source so a
# second-hand reaction video cannot outrank the primary source it covers.
SOURCE_SCORE_MULTIPLIER = {
    "bilibili": 0.80,
}

SCORE_WEIGHTS = {
    "engagement": 0.08,
    "recency": 0.07,
    "code_evidence": 0.10,
    "author_credibility": 0.08,
    "cross_source": 0.07,
    "discussion_heat": 0.05,
    "practice_depth": 0.15,
    "reproducibility": 0.12,
    "info_density": 0.10,
    "originality": 0.10,
    "problem_solution_arc": 0.08,
}

# Source-level author credibility defaults (when author not in known_experts)
SOURCE_CREDIBILITY_DEFAULTS = {
    "hackernews": 0.5,
    "reddit": 0.4,
    "rss_blogs": 0.6,
    "github_trending": 0.7,
    "devto": 0.4,
    "lobsters": 0.5,
    "medium": 0.3,
    "twitter": 0.5,
    "bilibili": 0.3,
    "youtube": 0.4,
    "media": 0.6,
    "linkedin": 0.5,
}

FILTER_PROMPT = """你是一个 AI 工程实践内容筛选、分类与评分器。

## 你的目标
找出「**如何最好地使用 AI**」的实用经验——重点是帮助一线开发者/团队**用 AI 提升生产力**、**选对模型**、**用好工具与 Agent**。这是一份「best practice for USING AI」的精选，不是「如何构建/训练 AI」的合集。

## 内容优先级（决定分类与打分倾向）
- **最高价值（核心）**：AI 工具实战用法与工作流（Claude Code/Cursor/Copilot 等）、模型选型与对比（什么场景用哪个模型）、Prompt/Agent 工程、用 AI 提效的真实经验与踩坑。
- **次要**：团队流程、RAG/检索、复盘、开源工具实测。
- **最低（保留但排最后）**：从零训练模型、fine-tuning、量化、推理优化、企业基础设施、垂直行业落地——这些是「构建 AI」，对「用 AI 提效」帮助有限。
- 判断锚点：读者是「想更好地用 AI 干活的开发者」。内容能直接帮他用 AI 提效 → 核心；只是讲怎么造模型/训模型 → 归 deploy_finetune（最低优先级），不要错放进 personal_exp / personal_workflow。

## 通过标准（必须同时满足至少两条）
- 有具体场景：某人/团队用 AI 做了什么具体的事
- 有真实困难或洞察：过程中遇到了什么问题、发现了什么反直觉的东西
- 有解决路径或可借鉴的经验：最终怎么做的、效果如何

## 评分规则（1-10 分）
基础分 5 分，根据以下信号加减：

**加分信号（每项 +1~2）：**
- 有 GitHub 链接或开源代码 → +2
- 有具体数据指标（准确率、成本、时间对比） → +2
- 来自一线工程师/团队的亲身经历（非转述） → +1
- 有清晰的 before/after 对比 → +1
- 内容深度超过表面描述，有技术细节 → +1
- 高互动量（HN 100+分, Reddit 50+分, 视频 5万+播放） → +1

**减分信号（每项 -1~2）：**
- 标题党/夸张标题（"INSANE", "改变一切", "永远"） → -1
- 有明显商业推广/affiliate 链接 → -2
- 内容较浅，只讲 what 不讲 why/how → -1
- 信息来源为转述/二手信息 → -1

## 分类体系（共 12 类）

**核心：如何最好地使用 AI（最高优先级）**
- personal_workflow: AI 工具用法与工作流 — Claude Code/Cursor/Copilot 等工具的实战用法、提效工作流、工具链组合
- prompt_agent: Prompt/Agent 工程 — prompt 设计、agent 编排、tool use 的实操技巧
- tool_comparison: 模型/工具选型对比 — 有深度实测的模型能力对比、什么场景用哪个模型/工具（非泛泛 A vs B）
- personal_exp: 生产力提升经验 — 个人用 AI 提效的心得、踩坑、反直觉发现（注意：从零训练/微调模型不属于此类，归 deploy_finetune）

**实践：相邻动手工程（次要）**
- team_process: 团队协作/流程 — AI 融入团队开发流程、code review、CI/CD
- rag_retrieval: RAG/检索系统 — 向量库、检索增强生成、embedding 的工程实践
- postmortem: 故障/踩坑复盘 — 失败案例、Bug 分析、事故复盘、教训总结
- opensource_tool: 开源项目/工具 — 值得关注的新开源项目实测、源码解析

**构建：如何造/训 AI（保留但最低优先级，永远排最后）**
- enterprise_arch: 企业架构 — 企业级 AI 系统设计、基础设施选型、大规模部署
- ai_vertical: AI+垂直行业 — AI 在医疗/金融/法律/教育/制造等领域的真实落地
- deploy_finetune: 模型部署/微调/训练 — 从零训练模型、fine-tuning、量化、推理优化、本地部署实战（凡是「怎么造/训模型」一律归这里）

**花边（单独板块）：**
- novelty: 花边/创意实验 — 有趣但非工程实践：概念验证、创意 demo、游戏模拟、艺术项目。标准：确实用了 AI 做了有趣的事，但不包含可复用的工程经验。例如"20个AI agent在中世纪自主交易"、"AI 画梵高风格"、"用 GPT 玩 DnD"。

## 关键区分：工程实践 vs 花边
- "我用 AI agent 重构了代码库，踩坑经验" → 工程实践（personal_exp）
- "我让 20 个 AI agent 在游戏里自主交易" → 花边（novelty）
- "RAG 召回率从 60% 优化到 90%" → 工程实践（rag_retrieval）
- "我用 GPT 生成了一个完整的游戏" → 花边（novelty），除非有工程细节
- 判断标准：读者看完能否学到可复用的工程方法？能 → 工程实践，不能 → 花边

## 必须拒绝（score=0）
- 产品发布新闻："X 发布，得分超越人类"
- 白嫖/引流/破解："免费使用 GPT"、"不翻墙用 Claude"
- 泛泛对比测评：没有真实项目验证的 "A vs B 谁更强"
- 卖课营销："手把手教你"、"零基础入门"、"X 天精通"、免费课程推广
- 纯观点讨论：没有实操经验的预测和评论
- 纯教程/功能介绍：只讲功能、只演示 UI，没有真实踩坑经验
- 与 AI 无关的内容：关键词碰巧命中但实际内容无关

## GitHub 项目专项规则（严格）
GitHub Trending 来源的项目必须严格评判。一个 AI 相关的开源仓库本身不是"实践经验"。
- **通过标准**：README 中有作者的设计决策说明、技术选型对比、踩坑经验、性能对比数据。即：作者不只是发布工具，还分享了"为什么这样做"和"遇到了什么问题"。
- **拒绝标准**：只是功能列表 + 安装说明 + API 文档 → 拒绝。这是工具介绍，不是实践分享。
- **降分标准**：GitHub 项目即使通过，practice_depth 和 problem_solution_arc 分数也应更严格 — 除非 README 确实讲了深度经验。
- 示例：
  - "一个 RAG 框架，支持 5 种向量库" → 拒绝（功能介绍）
  - "我构建了一个 RAG 框架，以下是我对比 5 种向量库后的选择及踩坑" → 通过

## 边界判定
- "我构建了 X" 但只展示最终效果、没有讲困难和经验 → 拒绝
- "X 工具测评" 只跑 benchmark 没有在真实项目验证 → 拒绝
- "从零构建 X" 且讲了设计决策和踩坑 → 通过
- 企业 CTO/工程师分享真实落地经验 → 通过
- 简介中有 GitHub 仓库链接 → 加分信号（但 github_trending 来源不适用此条）

## 输入
{items}

## 输出格式
返回纯 JSON（不要 markdown 代码块）。对每个通过（pass）的条目，还需评估以下 5 个维度（0.0-1.0）：
- practice_depth：实操深度，有无真实踩坑/步骤/代码
- reproducibility：可复现性，读者能否按图索骥重现
- info_density：信息密度，单位篇幅包含的有效信息量
- originality：原创性，是否提供了新视角或独家经验
- problem_solution_arc：问题-解决弧，有无清晰的问题→分析→解决闭环

格式：
{{"results": [
  {{"index": 0, "decision": "pass" 或 "reject", "category": "类别ID",
    "scores": {{"practice_depth": 0.0-1.0, "reproducibility": 0.0-1.0, "info_density": 0.0-1.0, "originality": 0.0-1.0, "problem_solution_arc": 0.0-1.0}},
    "reason": "一句话理由（中文）"}}
]}}

类别 ID 必须从以下选择：enterprise_arch, personal_exp, personal_workflow, postmortem, tool_comparison, prompt_agent, deploy_finetune, rag_retrieval, ai_vertical, opensource_tool, team_process, novelty"""


SUMMARY_PROMPT = """你是一个技术内容总结专家。请基于以下视频字幕，提取核心要点。

## 视频信息
标题: {title}
作者: {author}
平台: {platform}
分类: {category}

## 字幕内容
{subtitles}

## 要求
请用中文输出，格式：
1. **核心问题**：这个视频要解决什么问题？（1-2句）
2. **关键发现/困难**：过程中遇到了什么问题或反直觉的发现？（2-3点）
3. **解决方案/经验**：最终如何解决的？有什么可借鉴的？（2-3点）
4. **一句话总结**：用一句话概括这个视频的价值

如果字幕内容不足以提取有价值的信息，直接说明。"""
