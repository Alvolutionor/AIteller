# src/prompts/filter.py
"""Shared filter/categorization/scoring prompts and category definitions."""

CATEGORIES = {
    "enterprise_arch":    "公司架构层面 — 企业级 AI 系统设计、基础设施选型、大规模部署",
    "personal_exp":       "个人使用经验 — 个人用 AI 工具的心得、踩坑、反直觉发现",
    "personal_workflow":  "个人实践工作流 — 完整的工作流分享、效率提升方法、工具链组合",
    "postmortem":         "故障/踩坑复盘 — 失败案例、Bug 分析、事故复盘、教训总结",
    "tool_comparison":    "工具对比评测 — 有深度实测数据的工具对比，非泛泛 A vs B",
    "prompt_agent":       "Prompt/Agent 工程 — prompt 设计、agent 编排、tool use 的实操技巧",
    "deploy_finetune":    "模型部署/微调 — 本地部署、fine-tuning、量化、推理优化实战",
    "rag_retrieval":      "RAG/检索系统 — 向量库、检索增强生成、embedding 的工程实践",
    "ai_vertical":        "AI+垂直行业 — AI 在医疗/金融/法律/教育/制造等领域的真实落地",
    "opensource_tool":    "开源项目/工具 — 值得关注的新开源项目实测、源码解析",
    "team_process":       "团队协作/流程 — AI 融入团队开发流程、code review、CI/CD",
    "novelty":            "花边/创意实验 — 有趣但非工程实践的创意 demo、概念验证、艺术项目",
}

CATEGORY_IDS = list(CATEGORIES.keys())

TIER_MAP = {
    # Tier 1: Specific practice experience
    "personal_exp": 1,
    "personal_workflow": 1,
    "postmortem": 1,
    "team_process": 1,
    # Tier 2: Technical with hands-on
    "prompt_agent": 2,
    "rag_retrieval": 2,
    "tool_comparison": 2,
    "opensource_tool": 2,
    # Tier 3: Macro / infrastructure
    "enterprise_arch": 3,
    "deploy_finetune": 3,
    "ai_vertical": 3,
    # Novelty: separate section
    "novelty": 0,
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
}

FILTER_PROMPT = """你是一个 AI 工程实践内容筛选、分类与评分器。

## 你的目标
找出「有人用 AI 解决了真实问题」的经验分享，归入类别，并打分。

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

**正经工程实践（Tier 1-3）：**
- enterprise_arch: 公司架构层面 — 企业级 AI 系统设计、基础设施选型、大规模部署
- personal_exp: 个人使用经验 — 个人用 AI 工具的心得、踩坑、反直觉发现
- personal_workflow: 个人实践工作流 — 完整的工作流分享、效率提升方法、工具链组合
- postmortem: 故障/踩坑复盘 — 失败案例、Bug 分析、事故复盘、教训总结
- tool_comparison: 工具对比评测 — 有深度实测数据的工具对比（非泛泛 A vs B）
- prompt_agent: Prompt/Agent 工程 — prompt 设计、agent 编排、tool use 的实操技巧
- deploy_finetune: 模型部署/微调 — 本地部署、fine-tuning、量化、推理优化实战
- rag_retrieval: RAG/检索系统 — 向量库、检索增强生成、embedding 的工程实践
- ai_vertical: AI+垂直行业 — AI 在医疗/金融/法律/教育/制造等领域的真实落地
- opensource_tool: 开源项目/工具 — 值得关注的新开源项目实测、源码解析
- team_process: 团队协作/流程 — AI 融入团队开发流程、code review、CI/CD

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
