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

OpenAI:

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python scripts/run_korea_toss_analysis.py \
  --provider openai \
  --ticker 005930.KS \
  --date 2026-07-08 \
  --model gpt-4.1-mini \
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
  --model gpt-4.1-mini \
  --max-order-amount 100000 \
  --execute
```

The Toss order action is derived from the Portfolio Manager rating:

- `Buy`, `Overweight` -> BUY
- `Hold` -> no order
- `Underweight`, `Sell` -> SELL plan

Generated reports and local cache are stored under `.local/`, which is ignored
by git.
