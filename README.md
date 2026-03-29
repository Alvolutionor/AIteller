# AIteller

AI 工程实践内容聚合 — 从多个来源自动采集、过滤、评分、总结 AI/LLM 领域的高质量实践经验，生成中文 PDF 日报/周报。

## Features

- **多源采集**: HackerNews, Reddit, GitHub Trending, arXiv, HuggingFace Papers, YouTube, Bilibili, Twitter/X, RSS Blogs, Dev.to, Lobsters, Medium
- **LLM 驱动过滤**: 基于 Claude/OpenAI/DeepSeek 的智能内容分类与筛选，聚焦「有人用 AI 解决了真实问题」的经验分享
- **11 维评分体系**: 实践深度、可复现性、信息密度、原创性、互动量、代码证据等（0-10 分）
- **三级分层**: Tier 1 个人实践 → Tier 2 工程技术 → Tier 3 宏观/基础设施 + 花边实验
- **中文 PDF 报告**: 日报（竖版 A4）与周报（横版双栏），按质量分层展示
- **多通道推送**: 邮件 SMTP、企业微信机器人、Slack Webhook

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/<your-username>/AIteller.git
cd AIteller
uv sync                    # Install dependencies (requires uv)

# 2. Configure
cp config/.env.example config/.env          # Fill in API keys
cp config/config.example.yaml config/config.yaml  # Customize settings

# 3. Run
uv run python -m src.main collect           # Collect from all sources
uv run python -m src.main report daily      # Generate daily PDF
uv run python -m src.main report weekly     # Generate weekly PDF
uv run python -m src.main send daily        # Send latest daily PDF
uv run python -m src.main status            # Check status
```

### More Commands

```bash
# Test a single source
uv run python -m src.main test-source hackernews
uv run python -m src.main test-source reddit

# Test notification channels
uv run python -m src.main test-notify email
uv run python -m src.main test-notify wechat

# Re-score existing items without re-collecting
uv run python -m src.main rescore
uv run python -m src.main rescore all       # Re-score everything

# Clean up old data
uv run python -m src.main cleanup
```

## Configuration

Copy `config/.env.example` to `config/.env` and fill in your values. All secrets use `${VAR_NAME}` substitution in `config/config.yaml`.

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_API_KEY` | At least one LLM key | Claude API key |
| `OPENAI_API_KEY` | Optional | OpenAI API key |
| `DEEPSEEK_API_KEY` | Optional | DeepSeek API key |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Optional | Reddit OAuth credentials |
| `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | Optional | Twitter/X session cookies |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` | Optional | SMTP email credentials |
| `SLACK_WEBHOOK_URL` | Optional | Slack notification webhook |
| `WECHAT_WEBHOOK_URL` | Optional | WeChat bot webhook |

## Project Structure

```
src/
├── collectors/     # Source collectors (HN, Reddit, YouTube, etc.)
├── processor/      # Filter, scorer, dedup, summarizer
├── prompts/        # LLM prompt templates (Chinese)
├── report/         # PDF report generators (daily & weekly)
├── notifiers/      # Slack, WeChat, Email channels
├── storage/        # SQLite database & migrations
└── utils/          # LLM client, content extractor, retry
config/
├── config.example.yaml   # Configuration template
├── .env.example          # Environment variables template
├── feeds.yaml            # Twitter/YouTube/Blog source feeds
└── known_experts.yaml    # Expert profiles for scoring boost
tests/                    # Test suite (pytest + pytest-asyncio)
```

## Scoring System

Each item is scored across 11 dimensions (0-10 scale):

| Dimension | Weight | Source |
|---|---|---|
| practice_depth | 15% | LLM |
| reproducibility | 12% | LLM |
| code_evidence | 10% | Deterministic |
| info_density | 10% | LLM |
| originality | 10% | LLM |
| engagement | 8% | Deterministic (per-source normalization) |
| author_credibility | 8% | Known experts list + source defaults |
| problem_solution_arc | 8% | LLM |
| recency | 7% | Deterministic |
| cross_source | 7% | Deterministic |
| discussion_heat | 5% | Deterministic |

PDF tiers: **必读** (≥6.0) → **精选** (≥5.0) → **推荐** (≥4.0) → **参考** (<4.0)

## Tech Stack

- **Language**: Python 3.11+ (asyncio)
- **Package Manager**: [uv](https://docs.astral.sh/uv/)
- **Database**: SQLite (WAL mode, FTS5)
- **LLM Providers**: Claude, OpenAI, DeepSeek, Ollama (local)
- **PDF**: fpdf2 with Chinese font support
- **HTTP**: aiohttp, feedparser, trafilatura, BeautifulSoup4

## License

MIT
