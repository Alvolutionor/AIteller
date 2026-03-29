# AIteller

An AI engineering practice content aggregator that automatically collects, filters, scores, and summarizes high-quality AI/LLM content from multiple sources, generating PDF reports (daily & weekly).

## Features

- **Multi-source collection**: HackerNews, Reddit, GitHub Trending, arXiv, HuggingFace Papers, YouTube, Bilibili, Twitter/X, RSS Blogs, Dev.to, Lobsters, Medium
- **LLM-driven filtering**: Intelligent content classification using Claude/OpenAI/DeepSeek — focuses on _"someone used AI to solve a real problem"_ practice sharing
- **11-dimension scoring**: Practice depth, reproducibility, information density, originality, engagement, code evidence, and more (0-10 scale)
- **Tiered categorization**: Tier 1 (hands-on practice) → Tier 2 (deep tech) → Tier 3 (big picture) + novelty experiments
- **PDF reports**: Daily (portrait A4) and weekly (landscape two-column), with `--top N` support
- **Multi-language**: English and Chinese PDF output (`--lang en|zh`)
- **Multi-channel delivery**: Email SMTP, WeChat bot, Slack webhook

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/Alvolutionor/AIteller.git
cd AIteller
uv sync

# 2. Configure
cp config/.env.example config/.env          # Fill in API keys
cp config/config.example.yaml config/config.yaml

# 3. Run
uv run python -m src.main collect           # Collect from all sources
uv run python -m src.main report weekly     # Generate weekly PDF (English, top 100)
uv run python -m src.main report weekly --lang zh  # Chinese version
uv run python -m src.main report daily      # Generate daily PDF
uv run python -m src.main send weekly       # Send latest PDF via email/WeChat/Slack
uv run python -m src.main status            # Check pipeline status
```

### CLI Reference

```bash
# Collection
uv run python -m src.main collect           # Collect + filter + score
uv run python -m src.main rescore           # Re-filter & re-score without re-collecting
uv run python -m src.main rescore all       # Re-score everything in DB

# Reports
uv run python -m src.main report weekly --top 50    # Top 50 items
uv run python -m src.main report weekly --top 0     # All items (no limit)
uv run python -m src.main report daily --lang zh    # Chinese daily

# Testing
uv run python -m src.main test-source hackernews    # Test single source
uv run python -m src.main test-notify email         # Test notification channel

# Maintenance
uv run python -m src.main cleanup           # Clean up old data
```

## Configuration

Copy `config/.env.example` to `config/.env` and fill in your values. All secrets use `${VAR_NAME}` substitution in `config/config.yaml`.

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_API_KEY` | At least one LLM key | Claude API key |
| `OPENAI_API_KEY` | Optional | OpenAI API key |
| `DEEPSEEK_API_KEY` | Optional | DeepSeek API key |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Optional | Reddit OAuth |
| `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | Optional | Twitter/X session |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` | Optional | SMTP email |
| `SLACK_WEBHOOK_URL` | Optional | Slack webhook |
| `WECHAT_WEBHOOK_URL` | Optional | WeChat bot webhook |

## Architecture

```
src/
├── collectors/     # Source collectors (HN, Reddit, YouTube, etc.)
├── processor/      # Filter, scorer, dedup, summarizer
├── prompts/        # LLM prompt templates
├── report/         # PDF generators (daily & weekly) + i18n
├── notifiers/      # Email, WeChat, Slack channels
├── storage/        # SQLite database & migrations
└── utils/          # LLM client, content extractor, retry
config/
├── config.example.yaml   # Configuration template
├── .env.example          # Environment variables template
├── feeds.yaml            # Twitter/YouTube/Blog source feeds
└── known_experts.yaml    # Expert profiles for scoring boost
tests/                    # pytest + pytest-asyncio
```

## Scoring System

Each item is evaluated across 11 dimensions (0-10 scale):

| Dimension | Weight | Source |
|---|---|---|
| Practice Depth | 15% | LLM |
| Reproducibility | 12% | LLM |
| Code Evidence | 10% | Deterministic |
| Info Density | 10% | LLM |
| Originality | 10% | LLM |
| Engagement | 8% | Deterministic (per-source normalization) |
| Author Credibility | 8% | Known experts list + source defaults |
| Problem-Solution Arc | 8% | LLM |
| Recency | 7% | Deterministic |
| Cross-Source | 7% | Deterministic |
| Discussion Heat | 5% | Deterministic |

**PDF tiers**: Must Read (>=6.0) → Featured (>=5.0) → Recommended (>=4.0) → Reference (<4.0)

## Tech Stack

- **Language**: Python 3.11+ (asyncio)
- **Package Manager**: [uv](https://docs.astral.sh/uv/)
- **Database**: SQLite (WAL mode, FTS5)
- **LLM**: Claude, OpenAI, DeepSeek, Ollama (local)
- **PDF**: fpdf2 with CJK font support
- **HTTP**: aiohttp, feedparser, trafilatura, BeautifulSoup4

## License

MIT
