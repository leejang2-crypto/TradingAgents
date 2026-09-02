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

## Disabled Legacy Hybrid OpenAI + Ollama Mode

Hybrid mode is currently disabled in `main.py` because local Ollama became the
paper-trading bottleneck. Keep this section as rollback documentation only. To
restore local LLM use, re-enable `--provider` choices in `main.py`, restore the
commented `resolve_models()` hybrid/Ollama branch, and re-enable the commented
hybrid config block.

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

## Naver Candidate Discovery

Use this runner before analyzing a specific ticker when you want news-driven
candidate ideas. It queries Naver Search API with market/theme/signal/risk
keywords, extracts known Korean ticker mentions, scores them, and writes both a
machine-readable JSON file and a Markdown review report.

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python scripts/discover_naver_candidates.py \
  --end-date 2026-07-10 \
  --days 3 \
  --display 10 \
  --limit 20
```

Optional custom queries:

```bash
../.venv/bin/python scripts/discover_naver_candidates.py \
  --end-date 2026-07-10 \
  --extra-queries "HBM4 고객 인증,전력망 투자 변압기,방산 수출 계약"
```

Default keyword buckets:

- `market`: broad market, FX, rates, semiconductor index, investor flows
- `themes`: HBM, power equipment, defense, shipbuilding, bio, batteries, value-up
- `selection_signals`: earnings surprise, target-price upgrade, institutional/foreign buying, new orders, shareholder returns
- `risk_signals`: earnings shock, dilution, trading halt, litigation, demand slowdown

Outputs are saved under:

```text
.local/candidates/
```

Use the ranked candidates as the next `--korea-ticker` inputs for OpenAI
analysis. The scoring is deliberately simple and transparent: positive
signal mentions score higher, theme mentions add context, and risk mentions
subtract from the score.

## Automated Paper Trading

`main.py` now defaults to `auto-paper` with `--run-mode daily-market`.
Running only `main.py` keeps the process alive and performs the workflow once
per Korean regular market session, Monday-Friday, between 09:00 and 15:30
Asia/Seoul:

1. Naver keyword-based candidate discovery
2. Top candidate analysis with external OpenAI LLMs
3. Toss dry-run paper trade recording when the final rating maps to BUY/SELL

The current project objective is Korean-stock intraday paper trading with
10,000,000 KRW capital and a daily target return of 1%. This means the daily
paper profit target is 100,000 KRW. The default per-candidate BUY budget is
10% of paper capital, so 1,000,000 KRW. Agents receive this objective in the
Research Manager, Trader, and Portfolio Manager prompts so decisions prefer
liquid same-day setups with explicit entry, stop-loss, take-profit, and exit
discipline.

```bash
cd /Users/leejang2/Project/TradingAgents
/Users/leejang2/Project/.venv/bin/python main.py
```

If the process starts before 09:00, after 15:30, or on a weekend, it waits for
the next regular session. It records completed dates in:

```text
.local/scheduler/auto_paper_daily_runs.json
```

That file prevents accidental duplicate paper-trading runs when the process is
left running or restarted on the same trading day.

Equivalent explicit command:

```bash
/Users/leejang2/Project/.venv/bin/python main.py \
  --scenario auto-paper \
  --run-mode daily-market \
  --provider openai \
  --paper-capital 10000000 \
  --position-pct 0.10 \
  --daily-target-return 0.01 \
  --trading-style intraday-scalping \
  --trade-candidates 3
```

For a one-off immediate test run that exits after completion, use:

```bash
/Users/leejang2/Project/.venv/bin/python main.py --run-mode once
```

This never sends a real Toss order. Real orders still require:

```bash
--scenario korea-execute --yes
```

Auto-paper summary files are saved under:

```text
.local/auto_paper/
```

Dry-run order records are appended to:

```text
.local/paper_trades/trades.jsonl
```

Naver Search API calls are rate-limited before each request. The local quota
ledger uses a file lock so multiple local TradingAgents processes share the
same per-second and daily counters:

```text
.local/cache/naver_search_quota.json
```

Default limits are intentionally conservative:

```text
naver_rate_limit_per_second = 9
naver_rate_limit_daily = 24000
naver_rate_limit_retries = 2
```

This mirrors the Redis quota idea from the 429 troubleshooting article, but
uses a local file-backed ledger for the current Mac-only setup. If this later
runs across several machines, replace the file ledger with Redis `INCR`/expiry
using the same quota concepts.
