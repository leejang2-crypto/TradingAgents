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

## Selectable OpenAI + Ollama Mode

`main.py` supports `--provider openai`, `ollama`, and `hybrid`. Hybrid is the
default and routes only news to the local model by default.
After dashboard login, select providers/models and save. Existing runs retain
their settings; subsequent requests use the saved settings. Saving alone does
not start a run; the separate manual controls can request an additional run.

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python main.py \
  --scenario korea-dry-run \
  --provider hybrid \
  --ollama-quick-model qwen3:8b \
  --openai-deep-model gpt-5.5 \
  --korea-ticker 005930.KS \
  --date 2026-07-10
```

Default hybrid routing:

| Agent | Provider |
|---|---|
| market analyst | OpenAI |
| news analyst | Ollama |
| sentiment/fundamentals analysts | OpenAI (not selected by the default Korean runner) |
| bull researcher | OpenAI |
| bear researcher | OpenAI |
| research manager | OpenAI |
| trader | OpenAI |
| aggressive risk analyst | OpenAI |
| neutral risk analyst | OpenAI |
| conservative risk analyst | OpenAI |
| portfolio manager | OpenAI |

Hybrid mode enables local-safe validation. If the local/hybrid chain contains
mechanical failures such as ticker drift, an implausible stop loss, truncated
portfolio output, or conflicting final action signals, the final decision is
forced to `Hold` before Toss dry-run or execution planning.

The four independent model fields are `openai_quick_model`,
`openai_deep_model`, `ollama_quick_model`, and `ollama_deep_model`.
Research Manager and Portfolio Manager use the deep pool; other roles use
quick. In hybrid mode, `--quick-model`/`--deep-model` override the OpenAI pool.
Use the explicit `--ollama-quick-model`/`--ollama-deep-model` flags for local IDs.
The web page retrieves installed Ollama models from local `/api/tags`; it does
not download models. The two installed model IDs are `qwen3:8b` and `gemma3:12b`.

Local news gets one tool-free JSON call containing bounded news excerpts (up to
14,000 characters, output at most 1,600 tokens). It selects verbatim quotes and
classifies them as catalyst/risk/uncertainty/neutral. Python checks the ticker,
article index, and each quote against that exact source before generating the
report. Unverified names or free-form explanations are never published. Source
labels and URLs are taken from input data, not generated by the local model.
Other agents receive the resulting `news_report` through the same graph state.
This checks source fidelity, not the truth or completeness of the news.
It does not provide the cloud news agent's additional macro or
prediction-market tool coverage. Failed local calls remain errors, without an
automatic paid fallback. A token-limit-truncated local report is rejected.

For full details and review limitations, see
[the September review and setup guide](review_and_llm_settings_2026-09-10.md).

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

`auto-live --yes` uses the same candidate discovery, analysis, objective,
web settings, and daily scheduler. Only the final executor sends actual Toss
orders instead of dry-run records. `Start-Live.command` launches this mode
with the Tailscale dashboard. Stop the other launcher before switching modes.
Paper/live completion files and summary directories are separate. A live run
is durably marked started before analysis; interrupted runs are not replayed
automatically in the same morning/afternoon slot. Broker request/response events are recorded in
`.local/live_orders/orders.jsonl` and do not imply verified fills.
See [one-click launchers](one_click_launchers.md) for details and limitations.
Running only `main.py` keeps the process alive and performs the workflow at
09:20 and again at 13:00, with holdings monitoring from 09:00 to 15:30
Asia/Seoul:

1. Naver keyword-based candidate discovery
2. Top candidate analysis with hybrid LLMs (Ollama news, OpenAI other roles by default)
3. Deterministic 5m entry filters and risk sizing before BUY; simulated account fills or verified Toss orders

The current project objective is Korean-stock intraday paper trading with
10,000,000 KRW capital and a per-position gross price target of +1%.
There is no daily account profit quota. The default per-candidate BUY budget cap is
10% of paper capital, so 1,000,000 KRW. Agents receive this objective in the
Research Manager, Trader, and Portfolio Manager prompts so decisions prefer
liquid same-day setups with explicit entry, stop-loss, take-profit, and exit
discipline.
Position stops, daily loss limits and full-exit verification are described in
[the intraday strategy guide](intraday_strategy.md).

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
left running or restarted in the same time slot. A late start runs only the
current slot, not both slots back-to-back. See [twice-daily schedule](twice_daily_schedule.md).

Equivalent explicit command:

```bash
/Users/leejang2/Project/.venv/bin/python main.py \
  --scenario auto-paper \
  --run-mode daily-market \
  --provider hybrid \
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

## Web Dashboard and Settings

Start the web dashboard with the daily scheduler:

```bash
cd /Users/leejang2/Project/TradingAgents
/Users/leejang2/Project/.venv/bin/python main.py \
  --monitor \
  --monitor-host 0.0.0.0 \
  --monitor-port 8765
```

From the Mac mini itself, open:

```text
http://127.0.0.1:8765
```

From another device on the same network, open:

```text
http://<Mac-mini-LAN-IP>:8765
```

The dashboard shows runtime status, the next scheduled run, the latest
auto-paper summary, candidate ranking, and recent dry-run paper trades.
It also includes a settings form for:

- paper capital
- per-candidate position percentage
- daily target return
- trading style
- candidate count and lookback days
- Naver candidate categories and extra keywords
- market open/close time
- holiday dates
- LLM timeout

Web access requires login with the credentials registered locally using
`Setup-Web-Login.command`. Settings and account queries reuse the login session.
Legacy API bearer tokens remain supported. If `TRADINGAGENTS_MONITOR_TOKEN` is
not set, `main.py` creates that compatibility token under:

```text
.local/monitor/monitor_token.txt
```

The saved trading criteria are stored in:

```text
.local/config/trading_settings.json
```

The scheduler reloads this file before each market-hours check, so saved
settings apply to the next automatic paper-trading run.

The JSON APIs are also available:

```text
http://<Mac-mini-LAN-IP>:8765/api/status
http://<Mac-mini-LAN-IP>:8765/api/settings
```

### Tailscale Mobile-Data Access

For access from a phone using mobile data, install Tailscale on both the Mac
mini and the phone, then bind the monitor to the Mac's Tailscale IP:

```bash
cd /Users/leejang2/Project/TradingAgents
/Users/leejang2/Project/.venv/bin/python main.py \
  --monitor \
  --monitor-host tailscale \
  --monitor-port 8765
```

`main.py` resolves the Mac's Tailscale IPv4 address with:

```bash
tailscale ip -4
```

The startup log prints the exact URL, usually like:

```text
http://100.x.y.z:8765
```

Open that URL from the phone browser after logging in to the same Tailscale
tailnet. This keeps the monitor off the public internet and avoids router port
forwarding.

Log in with the registered web credentials. Both private reads and writes
require authentication. See [web login and manual runs](web_login_and_manual_runs.md).

Viewing the dashboard never sends an order. Manual trading follows the runner's
fixed mode and requires an additional web confirmation in live mode. CLI real orders still require:

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
