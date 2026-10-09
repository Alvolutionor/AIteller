# AIteller 使用说明

## 一键运行（推荐）

双击 `run_weekly.bat` 即可自动完成所有步骤。

## 手动逐步运行

在项目根目录打开终端（cmd / PowerShell / Git Bash），按顺序执行：

### Step 1: 数据采集
```bash
uv run python -m src.main collect
```
从各数据源采集过去 168 小时的数据，自动去重、筛选、评分。
耗时：10-30 分钟（取决于网络）。
注意：Reddit 已从主采集中关闭（`sources.reddit.enabled: false`），单独在 Step 1b 采集。

### Step 1b: Reddit 单独采集（RSS-only）
```bash
del /q data\reddit_progress.json   # 清除上一周期遗留的 checkpoint
set LOOKBACK_HOURS=168
uv run python run_reddit_recollect.py
```
Reddit 的 `.json` 端点被 403 封锁，改用加固后的 RSS-only 采集器（Retry-After backoff 消化 429）。
必须先清除 `data/reddit_progress.json`，否则上周期的 checkpoint 会让所有 sub 跳过。
耗时：约 1 小时（保守限速避免被封）。

### Step 1c: 对新采集的 Reddit 重新评分
```bash
uv run python -m src.main rescore
```
`run_reddit_recollect.py` 只入库 raw 行；`rescore` 负责筛选+评分（resume 模式只处理新行）。

### Step 2: 生成中文周报 PDF
```bash
uv run python -m src.main report weekly --lang zh
```
输出：`output/weekly/AI实践周报_YYYY-MM-DD_YYYY-MM-DD.pdf`

### Step 3: 生成英文周报 PDF
```bash
uv run python -m src.main report weekly --lang en
```
输出：`output/weekly/AI_Practice_Weekly_YYYY-MM-DD_YYYY-MM-DD.pdf`

### Step 4: 发送邮件
```bash
uv run python -m src.main send weekly
```
自动将中文 PDF 发给中文订阅者，英文 PDF 发给英文订阅者。

### Step 5: 生成音频 + 视频（仅中文）
```bash
uv run python -m src.main audio weekly --lang zh --video-mode slideshow
```
生成中文播客音频（MP3）和视频（MP4），含封面。**英文视频不再生成。**
输出目录：`output/weekly/audio/` 和 `output/weekly/video/`
耗时：5-15 分钟。

### Step 6: 查看状态
```bash
uv run python -m src.main status
```

## 单独测试某个数据源

```bash
uv run python -m src.main test-source hackernews
uv run python -m src.main test-source reddit
uv run python -m src.main test-source youtube
uv run python -m src.main test-source bilibili
uv run python -m src.main test-source twitter
uv run python -m src.main test-source github
uv run python -m src.main test-source arxiv
uv run python -m src.main test-source rss
uv run python -m src.main test-source hf_papers
```

## 重新评分（不重新采集）

```bash
# 重新评分最近 168h 的数据
uv run python -m src.main rescore

# 重新评分所有历史数据
uv run python -m src.main rescore all
```

## 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--lang zh/en/both` | 语言 | `en` |
| `--top N` | PDF/音频最多展示 N 条 | `100` |
| `--upload` | 音频生成后打开上传页面 | 关闭 |
| `--dry-run` | 跳过 LLM 调用 | 关闭 |

## 配置文件

- `config/config.yaml` — 主配置（数据源开关、LLM、通知等）
- `config/feeds.yaml` — RSS/YouTube/Twitter 订阅源
- `config/.env` — API 密钥和邮箱密码（不要提交到 git）
- `config/known_experts.yaml` — 已知专家列表（影响作者评分）

## 输出目录结构

```
output/
  weekly/
    AI实践周报_YYYY-MM-DD_YYYY-MM-DD.pdf      # 中文 PDF
    AI_Practice_Weekly_YYYY-MM-DD_YYYY-MM-DD.pdf  # 英文 PDF
    audio/
      weekly_YYYY-MM-DD_YYYY-MM-DD_zh.txt      # 中文播客脚本
      weekly_YYYY-MM-DD_YYYY-MM-DD_zh.mp3      # 中文音频
      weekly_YYYY-MM-DD_YYYY-MM-DD_en.txt      # 英文播客脚本
      weekly_YYYY-MM-DD_YYYY-MM-DD_en.mp3      # 英文音频
    video/
      cover_YYYY-MM-DD_YYYY-MM-DD_zh.png       # 中文封面
      cover_YYYY-MM-DD_YYYY-MM-DD_en.png       # 英文封面
      weekly_YYYY-MM-DD_YYYY-MM-DD_zh.mp4      # 中文视频
      weekly_YYYY-MM-DD_YYYY-MM-DD_en.mp4      # 英文视频
```
