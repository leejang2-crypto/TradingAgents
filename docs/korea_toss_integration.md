# Korea + Toss Integration

This project can analyze Korean tickers, enrich news with Naver Search API, and
prepare or execute Toss Invest Open API orders.

## Shared Environment

Keep shared secrets in `/Users/leejang2/Project/.env`.

Required for analysis:

```bash
OPENAI_API_KEY=...
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
```

Required for real Toss orders:

```bash
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
TOSS_ACCOUNT_SEQ=...
```

Optional:

```bash
TRADINGAGENTS_NEWS_DATA_VENDOR=naver,yfinance
TRADINGAGENTS_TOSS_MAX_ORDER_AMOUNT=100000
```

## Dry Run

Run Korean-stock analysis and execute a paper trade without sending a real Toss
order. No user confirmation is required for this dry-run path.

Before the agent graph runs, the Korea runner pre-collects a Naver Search API
news snapshot for the target ticker and stores it under `.local/news/`. The
Naver news vendor then reads that frozen snapshot first, so the news analyst,
sentiment analyst, research debate, trader, and risk agents all work from the
same collected news context through `news_report` / `sentiment_report`.

OpenAI:

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python scripts/run_korea_toss_analysis.py \
  --provider openai \
  --ticker 005930.KS \
  --date 2026-07-08 \
  --model gpt-5.5 \
  --max-order-amount 100000
```

Local Ollama:

```bash
brew install --cask ollama
ollama pull qwen3:8b

cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python scripts/run_korea_toss_analysis.py \
  --provider ollama \
  --model qwen3:8b \
  --ticker 005930.KS \
  --date 2026-07-08 \
  --max-order-amount 100000
```

Mixed local models:

```bash
../.venv/bin/python scripts/run_korea_toss_analysis.py \
  --provider ollama \
  --quick-model gemma3:4b \
  --deep-model qwen3:8b \
  --ticker 005930.KS \
  --date 2026-07-08
```

Ticker conventions:

- TradingAgents/Yahoo analysis: `005930.KS`, `035720.KQ`
- Toss order symbol: automatically converted to `005930`, `035720`

Dry-run paper trades are appended to:

```text
.local/paper_trades/trades.jsonl
```

If the final rating is `Hold`, no paper trade is recorded because there is no
order payload.

## Real Order

Real Toss orders are never sent unless `--execute` is provided. Without `--yes`,
the script also requires typing `YES`.

```bash
../.venv/bin/python scripts/run_korea_toss_analysis.py \
  --provider openai \
  --ticker 005930.KS \
  --date 2026-07-08 \
  --model gpt-5.5 \
  --max-order-amount 100000 \
  --execute
```

The Toss order action is derived from the Portfolio Manager rating:

- `Buy`, `Overweight` -> BUY
- `Hold` -> no order
- `Underweight`, `Sell` -> SELL plan

Generated reports and local cache are stored under `.local/`, which is ignored
by git.

## OpenAI vs Local Ollama Comparison

Use the comparison runner when you want to compare each agent's output between
OpenAI and locally installed Ollama models. The runner collects one shared Naver
news snapshot first, then runs both providers against the same ticker/date/news
context and writes a side-by-side Markdown report.

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python scripts/compare_openai_ollama.py \
  --ticker 005930.KS \
  --date 2026-07-10 \
  --openai-model gpt-5.5 \
  --ollama-quick-model qwen3:8b \
  --ollama-deep-model gemma3:12b
```

For a faster local comparison, use `qwen3:8b` for both local quick and deep
models:

```bash
../.venv/bin/python scripts/compare_openai_ollama.py \
  --ticker 005930.KS \
  --date 2026-07-10 \
  --ollama-quick-model qwen3:8b \
  --ollama-deep-model qwen3:8b
```

Comparison artifacts are saved under:

```text
.local/comparisons/
```

## Hybrid OpenAI + Ollama Mode

Use hybrid mode to reduce OpenAI token usage while keeping final trading
decisions on the more reliable OpenAI path. Ollama drafts lower-risk analysis;
OpenAI handles decision-critical synthesis and portfolio output.

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python main.py \
  --scenario korea-dry-run \
  --provider hybrid \
  --quick-model qwen3:8b \
  --deep-model gpt-5.5 \
  --korea-ticker 005930.KS \
  --date 2026-07-10
```

Default hybrid routing:

| Agent | Provider |
|---|---|
| market analyst | Ollama |
| news analyst | Ollama |
| sentiment/fundamentals analysts | Ollama |
| bull researcher | Ollama |
| bear researcher | Ollama |
| research manager | OpenAI |
| trader | OpenAI |
| aggressive risk analyst | Ollama |
| neutral risk analyst | Ollama |
| conservative risk analyst | OpenAI |
| portfolio manager | OpenAI |

Hybrid mode enables local-safe validation. If the local/hybrid chain contains
mechanical failures such as ticker drift, an implausible stop loss, truncated
portfolio output, or conflicting final action signals, the final decision is
forced to `Hold` before Toss dry-run or execution planning.
