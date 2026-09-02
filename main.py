from __future__ import annotations

import argparse
import asyncio
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.dataflows.naver_news import (
    collect_candidate_news_naver,
    collect_news_naver,
    rank_candidate_tickers,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = PROJECT_DIR / ".local"
DEFAULT_PAPER_CAPITAL = "10000000"
DEFAULT_POSITION_PCT = "0.10"
DEFAULT_DAILY_TARGET_RETURN = "0.01"
DEFAULT_TRADING_STYLE = "intraday-scalping"
KOREA_TZ = ZoneInfo("Asia/Seoul")
DEFAULT_MARKET_OPEN = "09:00"
DEFAULT_MARKET_CLOSE = "15:30"
DEFAULT_MONITOR_HOST = "127.0.0.1"
DEFAULT_MONITOR_PORT = 8765


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "TradingAgents integrated runner. By default it discovers Korean "
            "stock candidates and runs paper trading only."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["auto-paper", "all-safe", "tests", "minimal", "korea-dry-run", "korea-execute"],
        default="auto-paper",
        help=(
            "Execution scenario. auto-paper discovers candidates and records "
            "paper trades only; real Toss orders require korea-execute --yes."
        ),
    )
    parser.add_argument("--ticker", default="AAPL", help="Ticker for the minimal scenario.")
    parser.add_argument("--korea-ticker", default="005930.KS", help="Korean ticker, e.g. 005930.KS.")
    parser.add_argument(
        "--date",
        default=None,
        help=(
            "Analysis date YYYY-MM-DD. Default: today in Asia/Seoul. "
            "In daily-market mode, omit this so each trading day uses its own date."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["openai"],
        default="openai",
        help=(
            "LLM provider. Local Ollama/hybrid routing is currently disabled "
            "for paper trading because it was the execution bottleneck."
        ),
    )
    parser.add_argument("--model", help="Use one model for both quick/deep agents.")
    parser.add_argument("--quick-model", help="Override quick analyst/debate model.")
    parser.add_argument("--deep-model", help="Override deeper manager/portfolio model.")
    parser.add_argument(
        "--backend-url",
        help="Optional backend URL. Ollama default: http://localhost:11434/v1.",
    )
    parser.add_argument("--language", default="Korean")
    parser.add_argument(
        "--paper-capital",
        default=DEFAULT_PAPER_CAPITAL,
        help="Starting paper-trading capital in KRW. Default: 10,000,000.",
    )
    parser.add_argument(
        "--position-pct",
        default=DEFAULT_POSITION_PCT,
        help="Per-candidate BUY budget as a fraction of paper capital. Default: 0.10.",
    )
    parser.add_argument(
        "--daily-target-return",
        default=DEFAULT_DAILY_TARGET_RETURN,
        help="Daily paper-trading target return. Default: 0.01, i.e. 1%%.",
    )
    parser.add_argument(
        "--trading-style",
        default=DEFAULT_TRADING_STYLE,
        help="Trading objective style used in agent prompts. Default: intraday-scalping.",
    )
    parser.add_argument(
        "--max-order-amount",
        default=None,
        help="Override KRW budget per BUY dry-run. Default: paper-capital * position-pct.",
    )
    parser.add_argument("--candidate-limit", type=int, default=20, help="Candidate ranking size to save.")
    parser.add_argument("--trade-candidates", type=int, default=3, help="Top candidate count to analyze/trade.")
    parser.add_argument("--candidate-days", type=int, default=3, help="Candidate news lookback days.")
    parser.add_argument(
        "--candidate-categories",
        default="market,themes,selection_signals,risk_signals",
        help="Comma-separated Naver candidate keyword buckets.",
    )
    parser.add_argument("--extra-queries", default="", help="Comma-separated extra candidate news queries.")
    parser.add_argument("--news-display", type=int, default=5, help="Naver news collection count.")
    parser.add_argument(
        "--run-mode",
        choices=["daily-market", "once"],
        default="daily-market",
        help=(
            "auto-paper run mode. daily-market keeps main.py alive and runs "
            "once per Korean regular session; once runs immediately and exits."
        ),
    )
    parser.add_argument(
        "--market-open",
        default=DEFAULT_MARKET_OPEN,
        help="Korean regular-session open time HH:MM. Default: 09:00.",
    )
    parser.add_argument(
        "--market-close",
        default=DEFAULT_MARKET_CLOSE,
        help="Korean regular-session close time HH:MM. Default: 15:30.",
    )
    parser.add_argument(
        "--scheduler-poll-seconds",
        type=int,
        default=60,
        help="Sleep interval while waiting for market hours. Default: 60.",
    )
    parser.add_argument(
        "--holiday-dates",
        default=os.environ.get("TRADINGAGENTS_MARKET_HOLIDAYS", ""),
        help=(
            "Comma-separated market holiday dates YYYY-MM-DD to skip. "
            "Can also be set via TRADINGAGENTS_MARKET_HOLIDAYS."
        ),
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        default=os.environ.get("TRADINGAGENTS_MONITOR", "").strip().lower()
        in {"1", "true", "yes", "on"},
        help="Start a read-only web monitor for main.py status and paper-trading artifacts.",
    )
    parser.add_argument(
        "--monitor-host",
        default=os.environ.get("TRADINGAGENTS_MONITOR_HOST", DEFAULT_MONITOR_HOST),
        help=(
            "Web monitor bind host. Use 'tailscale' to bind to the Mac's "
            "Tailscale IPv4 address, or 0.0.0.0 for LAN access."
        ),
    )
    parser.add_argument(
        "--monitor-port",
        type=int,
        default=int(os.environ.get("TRADINGAGENTS_MONITOR_PORT", DEFAULT_MONITOR_PORT)),
        help="Web monitor port. Default: 8765.",
    )
    parser.add_argument(
        "--llm-timeout",
        default="180",
        help="Per-request LLM timeout in seconds for automated runs. Default: 180.",
    )
    parser.add_argument(
        "--skip-news-collection",
        action="store_true",
        help="Do not pre-collect Naver stock news before Korean-stock analysis.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --scenario korea-execute to skip the real-order confirmation prompt.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="With --scenario all-safe, skip the focused pytest suite.",
    )
    return parser


def resolve_models(args: argparse.Namespace) -> tuple[str, str, str | None]:
    # LOCAL LLM ROLLBACK NOTE:
    # The previous default was hybrid:
    #   if args.provider == "hybrid":
    #       quick_model = args.quick_model or args.model or "qwen3:8b"
    #       deep_model = args.deep_model or args.model or "gpt-5.5"
    #       backend_url = args.backend_url or "http://localhost:11434/v1"
    #       return quick_model, deep_model, backend_url
    # And Ollama-only used:
    #   default_model = "qwen3:8b" if args.provider == "ollama" else "gpt-5.5"
    # Restore those branches and provider choices above to re-enable local LLMs.
    default_model = "gpt-5.5"
    quick_model = args.quick_model or args.model or default_model
    deep_model = args.deep_model or args.model or default_model
    return quick_model, deep_model, args.backend_url


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid HH:MM time: {value}") from exc


def today_korea() -> date:
    return datetime.now(KOREA_TZ).date()


def holiday_dates(value: str | None) -> set[date]:
    days: set[date] = set()
    for item in split_csv(value or ""):
        days.add(date.fromisoformat(item))
    return days


def is_trading_day(day: date, holidays: set[date] | None = None) -> bool:
    return day.weekday() < 5 and day not in (holidays or set())


def market_window_for(day: date, open_text: str, close_text: str) -> tuple[datetime, datetime]:
    market_open = datetime.combine(day, parse_hhmm(open_text), tzinfo=KOREA_TZ)
    market_close = datetime.combine(day, parse_hhmm(close_text), tzinfo=KOREA_TZ)
    if market_close <= market_open:
        raise ValueError("--market-close must be later than --market-open.")
    return market_open, market_close


def is_regular_market_open(
    now: datetime,
    *,
    open_text: str = DEFAULT_MARKET_OPEN,
    close_text: str = DEFAULT_MARKET_CLOSE,
    holidays: set[date] | None = None,
) -> bool:
    local_now = now.astimezone(KOREA_TZ)
    if not is_trading_day(local_now.date(), holidays):
        return False
    market_open, market_close = market_window_for(local_now.date(), open_text, close_text)
    return market_open <= local_now <= market_close


def next_regular_market_open(
    now: datetime,
    *,
    open_text: str = DEFAULT_MARKET_OPEN,
    close_text: str = DEFAULT_MARKET_CLOSE,
    holidays: set[date] | None = None,
) -> datetime:
    local_now = now.astimezone(KOREA_TZ)
    day = local_now.date()
    while True:
        market_open, market_close = market_window_for(day, open_text, close_text)
        if is_trading_day(day, holidays) and local_now <= market_close:
            if local_now <= market_open:
                return market_open
            return local_now
        day += timedelta(days=1)
        local_now = datetime.combine(day, time.min, tzinfo=KOREA_TZ)


def scheduler_state_path() -> Path:
    return LOCAL_DIR / "scheduler" / "auto_paper_daily_runs.json"


def monitor_status_path() -> Path:
    return LOCAL_DIR / "monitor" / "main_status.json"


def load_scheduler_state() -> dict[str, Any]:
    path = scheduler_state_path()
    if not path.exists():
        return {"completedDates": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"completedDates": []}


def mark_scheduler_completed(run_date: str, summary_path: str | None) -> None:
    path = scheduler_state_path()
    state = load_scheduler_state()
    completed = set(state.get("completedDates", []))
    completed.add(run_date)
    state["completedDates"] = sorted(completed)
    state.setdefault("runs", {})[run_date] = {
        "completedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        "summaryPath": summary_path,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def has_scheduler_completed(run_date: str) -> bool:
    return run_date in set(load_scheduler_state().get("completedDates", []))


def write_monitor_status(status: str, **extra: Any) -> None:
    payload = {
        "status": status,
        "updatedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        **extra,
    }
    path = monitor_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def read_tail_jsonl(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def build_monitor_snapshot() -> dict[str, Any]:
    latest_summary_path = latest_file(LOCAL_DIR / "auto_paper", "auto_paper_*.json")
    latest_candidate_path = latest_file(LOCAL_DIR / "candidates", "auto_paper_candidates_*.json")
    latest_summary = read_json_file(latest_summary_path) if latest_summary_path else None
    latest_candidates = read_json_file(latest_candidate_path) if latest_candidate_path else None
    trades = read_tail_jsonl(LOCAL_DIR / "paper_trades" / "trades.jsonl", limit=30)
    return {
        "now": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "runtime": read_json_file(monitor_status_path()) or {},
        "scheduler": read_json_file(scheduler_state_path()) or {"completedDates": []},
        "latestSummaryPath": str(latest_summary_path) if latest_summary_path else None,
        "latestSummary": latest_summary,
        "latestCandidatePath": str(latest_candidate_path) if latest_candidate_path else None,
        "latestCandidates": latest_candidates,
        "recentTrades": trades,
    }


def _compact_text(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def render_monitor_html(snapshot: dict[str, Any]) -> str:
    runtime = snapshot.get("runtime") or {}
    summary = snapshot.get("latestSummary") if isinstance(snapshot.get("latestSummary"), dict) else {}
    candidates = snapshot.get("latestCandidates") if isinstance(snapshot.get("latestCandidates"), dict) else {}
    candidate_rows = []
    for row in (candidates.get("candidates") or [])[:10]:
        candidate_rows.append(
            "<tr>"
            f"<td>{_compact_text(row.get('ticker'))}</td>"
            f"<td>{_compact_text(row.get('name'))}</td>"
            f"<td>{_compact_text(row.get('score'))}</td>"
            f"<td>{_compact_text(row.get('mentionCount'))}</td>"
            f"<td>{_compact_text(row.get('positiveSignalCount'))}</td>"
            f"<td>{_compact_text(row.get('riskSignalCount'))}</td>"
            "</tr>"
        )
    trade_rows = []
    for trade in reversed(snapshot.get("recentTrades") or []):
        plan = trade.get("plan") or {}
        trade_rows.append(
            "<tr>"
            f"<td>{_compact_text(trade.get('recordedAt'))}</td>"
            f"<td>{_compact_text(plan.get('symbol'))}</td>"
            f"<td>{_compact_text(plan.get('action'))}</td>"
            f"<td>{_compact_text(plan.get('order_amount') or plan.get('quantity'))}</td>"
            f"<td>{_compact_text(plan.get('rating'))}</td>"
            "</tr>"
        )
    analysis_rows = []
    for analysis in (summary.get("analyses") or []):
        toss = analysis.get("toss") or {}
        plan = toss.get("plan") or {}
        analysis_rows.append(
            "<tr>"
            f"<td>{_compact_text(analysis.get('ticker'))}</td>"
            f"<td>{_compact_text(analysis.get('candidateRank'))}</td>"
            f"<td>{_compact_text(analysis.get('decision'))}</td>"
            f"<td>{_compact_text(plan.get('action'))}</td>"
            f"<td>{_compact_text(analysis.get('reportPath'))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>TradingAgents Monitor</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f8fa; color: #1d2433; }}
    header {{ background: #12343b; color: white; padding: 22px clamp(16px, 4vw, 44px); }}
    main {{ padding: 22px clamp(16px, 4vw, 44px); display: grid; gap: 18px; }}
    section {{ background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 16px; overflow-x: auto; }}
    h1 {{ margin: 0; font-size: 24px; }}
    h2 {{ margin: 0 0 12px; font-size: 17px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric {{ border-left: 4px solid #2f7d6d; background: #f2fbf8; padding: 10px 12px; }}
    .label {{ color: #5a667a; font-size: 12px; }}
    .value {{ margin-top: 4px; font-size: 18px; font-weight: 700; word-break: break-word; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e9f0; padding: 9px; text-align: left; vertical-align: top; }}
    th {{ color: #4c5a70; background: #f7f8fa; }}
    code {{ color: #40516d; word-break: break-all; }}
  </style>
</head>
<body>
  <header>
    <h1>TradingAgents Monitor</h1>
    <div>Asia/Seoul {_compact_text(snapshot.get("now"))} · 30초 자동 새로고침</div>
  </header>
  <main>
    <section>
      <h2>Runtime</h2>
      <div class="grid">
        <div class="metric"><div class="label">상태</div><div class="value">{_compact_text(runtime.get("status"))}</div></div>
        <div class="metric"><div class="label">업데이트</div><div class="value">{_compact_text(runtime.get("updatedAt"))}</div></div>
        <div class="metric"><div class="label">다음 실행</div><div class="value">{_compact_text(runtime.get("nextRunAfter"))}</div></div>
        <div class="metric"><div class="label">오늘 실행일</div><div class="value">{_compact_text(runtime.get("date") or summary.get("date"))}</div></div>
      </div>
    </section>
    <section>
      <h2>Objective</h2>
      <div class="grid">
        <div class="metric"><div class="label">자본금</div><div class="value">{_compact_text(summary.get("paperCapital"))} KRW</div></div>
        <div class="metric"><div class="label">일 목표 수익</div><div class="value">{_compact_text(summary.get("dailyTargetProfit"))} KRW</div></div>
        <div class="metric"><div class="label">종목당 예산</div><div class="value">{_compact_text(summary.get("perTradeBudget"))} KRW</div></div>
        <div class="metric"><div class="label">운영 모드</div><div class="value">{_compact_text(summary.get("runMode"))}</div></div>
      </div>
    </section>
    <section>
      <h2>Latest Analyses</h2>
      <table><thead><tr><th>Ticker</th><th>Rank</th><th>Decision</th><th>Order</th><th>Report</th></tr></thead>
      <tbody>{''.join(analysis_rows) or '<tr><td colspan="5">No analysis yet</td></tr>'}</tbody></table>
      <p><code>{_compact_text(snapshot.get("latestSummaryPath"))}</code></p>
    </section>
    <section>
      <h2>Candidate Ranking</h2>
      <table><thead><tr><th>Ticker</th><th>Name</th><th>Score</th><th>Mentions</th><th>Positive</th><th>Risk</th></tr></thead>
      <tbody>{''.join(candidate_rows) or '<tr><td colspan="6">No candidates yet</td></tr>'}</tbody></table>
      <p><code>{_compact_text(snapshot.get("latestCandidatePath"))}</code></p>
    </section>
    <section>
      <h2>Recent Paper Trades</h2>
      <table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Amount/Qty</th><th>Rating</th></tr></thead>
      <tbody>{''.join(trade_rows) or '<tr><td colspan="5">No paper trades yet</td></tr>'}</tbody></table>
    </section>
  </main>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = json.dumps(build_monitor_snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path in {"/", "/index.html"}:
            payload = render_monitor_html(build_monitor_snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_monitor_server(host: str, port: int) -> ThreadingHTTPServer:
    bind_host, access_host = resolve_monitor_host(host)
    server = ThreadingHTTPServer((bind_host, port), MonitorHandler)
    server.monitor_url = f"http://{access_host}:{port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    write_monitor_status(
        "monitor-started",
        monitorUrl=f"http://{access_host}:{port}",
        monitorBindHost=bind_host,
        monitorHostMode=host,
    )
    return server


def resolve_monitor_host(host: str) -> tuple[str, str]:
    if host.strip().lower() != "tailscale":
        return host, host
    ip_address = find_tailscale_ipv4()
    if not ip_address:
        raise RuntimeError(
            "Could not resolve a Tailscale IPv4 address. Open Tailscale, sign "
            "in on this Mac, then run main.py with --monitor-host tailscale again."
        )
    return ip_address, ip_address


def find_tailscale_ipv4() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        result = None
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]

    try:
        ifconfig = subprocess.run(
            ["ifconfig"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if ifconfig.returncode != 0:
        return None
    tailscale_network = ipaddress.ip_network("100.64.0.0/10")
    for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", ifconfig.stdout):
        candidate = ipaddress.ip_address(match.group(1))
        if candidate in tailscale_network:
            return str(candidate)
    return None


def order_budget(args: argparse.Namespace) -> Decimal:
    if args.max_order_amount:
        return Decimal(args.max_order_amount)
    capital = Decimal(args.paper_capital)
    position_pct = Decimal(args.position_pct)
    return (capital * position_pct).quantize(Decimal("1"))


def daily_profit_target(args: argparse.Namespace) -> Decimal:
    capital = Decimal(args.paper_capital)
    target_return = Decimal(args.daily_target_return)
    return (capital * target_return).quantize(Decimal("1"))


def trading_objective(args: argparse.Namespace) -> dict[str, str]:
    return {
        "style": args.trading_style,
        "capital_krw": str(Decimal(args.paper_capital).quantize(Decimal("1"))),
        "daily_target_return": str(Decimal(args.daily_target_return)),
        "daily_target_profit_krw": str(daily_profit_target(args)),
        "per_trade_budget_krw": str(order_budget(args)),
        "holding_period": "same-day only; avoid overnight exposure unless the final risk decision explicitly says HOLD",
        "risk_note": (
            "This is a paper-trading objective, not a guarantee. Agents should "
            "prefer liquid Korean stocks with clear intraday catalysts, define "
            "entry/stop/exit levels, and avoid chasing illiquid or already-spiked names."
        ),
    }


def make_config(
    args: argparse.Namespace,
    *,
    korea_mode: bool,
    minimal_mode: bool,
    collected_news_path: str | None = None,
) -> dict[str, Any]:
    quick_model, deep_model, backend_url = resolve_models(args)
    memory_path = LOCAL_DIR / "memory" / "trading_memory.md"
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": args.provider,
            "deep_think_llm": deep_model,
            "quick_think_llm": quick_model,
            "backend_url": backend_url,
            # LOCAL LLM ROLLBACK NOTE:
            # Previous hybrid config used:
            #   "backend_url": None if args.provider == "hybrid" else backend_url,
            #   "ollama_backend_url": (
            #       backend_url if args.provider == "hybrid"
            #       else DEFAULT_CONFIG.get("ollama_backend_url")
            #   ),
            "ollama_backend_url": DEFAULT_CONFIG.get("ollama_backend_url"),
            "output_language": args.language,
            "results_dir": str(LOCAL_DIR / "logs"),
            "data_cache_dir": str(LOCAL_DIR / "cache"),
            "memory_log_path": str(memory_path),
            "checkpoint_enabled": False,
            "llm_timeout": args.llm_timeout,
            "trading_objective": trading_objective(args),
        }
    )
    # LOCAL LLM ROLLBACK NOTE:
    # Re-enable this block with provider="hybrid" to route draft agents to
    # Ollama and final decision agents to OpenAI again.
    # if args.provider == "hybrid":
    #     config.update(
    #         {
    #             "hybrid_ollama_quick_llm": quick_model,
    #             "hybrid_ollama_deep_llm": quick_model,
    #             "hybrid_openai_quick_llm": deep_model,
    #             "hybrid_openai_deep_llm": deep_model,
    #             "local_safe_validation": True,
    #         }
    #     )
    if minimal_mode:
        config.update(
            {
                "max_debate_rounds": 1,
                "max_risk_discuss_rounds": 1,
                "news_article_limit": 3,
                "global_news_article_limit": 3,
            }
        )
    if korea_mode:
        config.update(
            {
                "data_vendors": {
                    **DEFAULT_CONFIG["data_vendors"],
                    "news_data": "naver,yfinance",
                },
                "news_article_limit": 5,
                "global_news_article_limit": 5,
                "naver_news_collected_data_path": collected_news_path or "",
                "naver_news_use_collected": bool(collected_news_path),
                "naver_news_live_fallback": True,
            }
        )
    return config


def collect_korea_news_snapshot(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.skip_news_collection:
        return None
    end_date = datetime.strptime(args.date, "%Y-%m-%d")
    start_date = (end_date - timedelta(days=7)).strftime("%Y-%m-%d")
    output_path = (
        LOCAL_DIR
        / "news"
        / f"analysis_naver_{args.korea_ticker.replace('.', '_')}_{args.date}.json"
    )
    try:
        articles = collect_news_naver(
            args.korea_ticker,
            start_date,
            args.date,
            display=args.news_display,
            sort="date",
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "path": None,
            "articleCount": 0,
        }

    payload = {
        "source": "Naver Search API",
        "purpose": "TradingAgents pre-analysis news snapshot",
        "startDate": start_date,
        "endDate": args.date,
        "tickers": [args.korea_ticker],
        "articleCount": len(articles),
        "articles": articles,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "path": str(output_path),
        "articleCount": len(articles),
    }


def collect_candidate_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    start_date = (
        datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=args.candidate_days)
    ).strftime("%Y-%m-%d")
    categories = split_csv(args.candidate_categories)
    extra_queries = split_csv(args.extra_queries)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = LOCAL_DIR / "candidates" / f"auto_paper_candidates_{args.date}_{stamp}"
    try:
        articles = collect_candidate_news_naver(
            start_date,
            args.date,
            categories=categories,
            display=args.news_display,
            sort="date",
            extra_queries=extra_queries,
        )
        candidates = rank_candidate_tickers(articles, limit=args.candidate_limit)
        ok = True
        error = None
    except Exception as exc:
        # Candidate discovery is useful but non-critical. A Naver rate limit or
        # transient API failure should not stop paper trading; the caller will
        # fall back to --korea-ticker and continue with vendor fallback enabled.
        articles = []
        candidates = []
        ok = False
        error = str(exc)

    payload = {
        "source": "Naver Search API",
        "purpose": "TradingAgents auto-paper candidate discovery",
        "ok": ok,
        "error": error,
        "startDate": start_date,
        "endDate": args.date,
        "categories": categories,
        "extraQueries": extra_queries,
        "articleCount": len(articles),
        "candidateCount": len(candidates),
        "candidates": candidates,
        "articles": articles,
    }
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md_path = output_prefix.with_suffix(".md")
    lines = [
        "# Auto Paper Candidate Discovery",
        "",
        f"- Period: {start_date} to {args.date}",
        f"- Paper capital: {args.paper_capital} KRW",
        f"- Per-trade budget: {order_budget(args)} KRW",
        f"- Daily target return: {Decimal(args.daily_target_return):.2%}",
        f"- Daily target profit: {daily_profit_target(args)} KRW",
        f"- Trading style: {args.trading_style}",
        f"- Article count: {len(articles)}",
        f"- Candidate count: {len(candidates)}",
        "",
        "| Rank | Ticker | Name | Score | Mentions | Positive | Risk |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(candidates, start=1):
        lines.append(
            f"| {index} | {row['ticker']} | {row['name']} | {row['score']} | "
            f"{row['mentionCount']} | {row['positiveSignalCount']} | {row['riskSignalCount']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "ok": ok,
        "error": error,
        "startDate": start_date,
        "endDate": args.date,
        "articleCount": len(articles),
        "candidateCount": len(candidates),
        "jsonPath": str(json_path),
        "markdownPath": str(md_path),
        "candidates": candidates,
    }


def run_focused_tests() -> dict[str, Any]:
    tests = [
        "tests/test_main_auto_paper.py",
        "tests/test_llm_hard_timeout.py",
        "tests/test_hybrid_provider.py",
        "tests/test_naver_vendor.py",
        "tests/test_toss_executor.py",
        "tests/test_ollama_base_url.py",
        "tests/test_vendor_routing.py",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "scenario": "tests",
        "ok": result.returncode == 0,
        "returnCode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def run_minimal(args: argparse.Namespace) -> dict[str, Any]:
    quick_model, deep_model, backend_url = resolve_models(args)
    graph = TradingAgentsGraph(
        selected_analysts=("market",),
        debug=False,
        config=make_config(args, korea_mode=False, minimal_mode=True),
    )
    final_state, decision = graph.propagate(args.ticker, args.date)
    report_path = graph.save_reports(final_state, args.ticker)
    return {
        "scenario": "minimal",
        "ticker": args.ticker,
        "date": args.date,
        "provider": args.provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "decision": decision,
        "reportPath": str(report_path),
    }


async def run_korea_toss(args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    if execute and not args.yes:
        raise SystemExit(
            "실제 Toss 주문은 --scenario korea-execute --yes 를 함께 지정해야 실행됩니다."
        )

    quick_model, deep_model, backend_url = resolve_models(args)
    collected_news = collect_korea_news_snapshot(args)
    collected_news_path = collected_news["path"] if collected_news and collected_news.get("ok") else None
    graph = TradingAgentsGraph(
        selected_analysts=("market", "news"),
        debug=False,
        config=make_config(
            args,
            korea_mode=True,
            minimal_mode=False,
            collected_news_path=collected_news_path,
        ),
    )
    final_state, decision = graph.propagate(args.korea_ticker, args.date)
    report_path = graph.save_reports(final_state, args.korea_ticker)
    plan = plan_from_decision(
        final_state.get("final_trade_decision") or decision,
        args.korea_ticker,
        max_order_amount=order_budget(args),
    )
    execution = await execute_plan(plan, execute=execute)
    return {
        "scenario": "korea-execute" if execute else "korea-dry-run",
        "ticker": args.korea_ticker,
        "date": args.date,
        "provider": args.provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "decision": decision,
        "reportPath": str(report_path),
        "collectedNews": collected_news,
        "toss": execution,
    }


async def run_auto_paper(args: argparse.Namespace) -> dict[str, Any]:
    if not args.date:
        args.date = today_korea().isoformat()
    quick_model, deep_model, backend_url = resolve_models(args)
    candidate_snapshot = collect_candidate_snapshot(args)
    candidates = candidate_snapshot.get("candidates", [])
    tickers = [row["ticker"] for row in candidates[: max(args.trade_candidates, 0)]]
    if not tickers and args.trade_candidates != 0:
        tickers = [args.korea_ticker]

    analyses: list[dict[str, Any]] = []
    original_ticker = args.korea_ticker
    try:
        for ticker in tickers:
            args.korea_ticker = ticker
            candidate_rank = next(
                (index for index, row in enumerate(candidates, start=1) if row["ticker"] == ticker),
                None,
            )
            try:
                result = await run_korea_toss(args, execute=False)
                result["candidateRank"] = candidate_rank
            except Exception as exc:
                result = {
                    "scenario": "korea-dry-run",
                    "ticker": ticker,
                    "date": args.date,
                    "provider": args.provider,
                    "candidateRank": candidate_rank,
                    "ok": False,
                    "error": str(exc),
                }
            analyses.append(result)
    finally:
        args.korea_ticker = original_ticker

    summary = {
        "scenario": "auto-paper",
        "date": args.date,
        "provider": args.provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "paperCapital": args.paper_capital,
        "positionPct": args.position_pct,
        "dailyTargetReturn": args.daily_target_return,
        "dailyTargetProfit": str(daily_profit_target(args)),
        "perTradeBudget": str(order_budget(args)),
        "tradingObjective": trading_objective(args),
        "runMode": args.run_mode,
        "marketHours": {
            "timezone": "Asia/Seoul",
            "open": args.market_open,
            "close": args.market_close,
        },
        "candidateDiscovery": {
            key: value
            for key, value in candidate_snapshot.items()
            if key != "candidates"
        },
        "selectedTickers": tickers,
        "analyses": analyses,
    }
    output_dir = LOCAL_DIR / "auto_paper"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"auto_paper_{args.date}_{datetime.now():%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["summaryPath"] = str(output_path)
    return summary


async def run_daily_market_scheduler(args: argparse.Namespace) -> int:
    holidays = holiday_dates(args.holiday_dates)
    startup_status = {
        "scheduler": "daily-market",
        "timezone": "Asia/Seoul",
        "marketOpen": args.market_open,
        "marketClose": args.market_close,
        "paperCapital": args.paper_capital,
        "dailyTargetReturn": args.daily_target_return,
        "dailyTargetProfit": str(daily_profit_target(args)),
        "perTradeBudget": str(order_budget(args)),
        "holidayDates": sorted(day.isoformat() for day in holidays),
    }
    write_monitor_status("scheduler-started", **startup_status)
    print(json.dumps({"status": "scheduler-started", **startup_status}, ensure_ascii=False), flush=True)
    while True:
        now = datetime.now(KOREA_TZ)
        run_date = now.date().isoformat()
        if is_regular_market_open(
            now,
            open_text=args.market_open,
            close_text=args.market_close,
            holidays=holidays,
        ):
            if has_scheduler_completed(run_date):
                next_open = next_regular_market_open(
                    now + timedelta(days=1),
                    open_text=args.market_open,
                    close_text=args.market_close,
                    holidays=holidays,
                )
                wait_seconds = max(1, int((next_open - now).total_seconds()))
                status_payload = {
                    "scheduler": "daily-market",
                    "date": run_date,
                    "nextRunAfter": next_open.isoformat(timespec="seconds"),
                }
                write_monitor_status("already-completed", **status_payload)
                print(
                    json.dumps({"status": "already-completed", **status_payload}, ensure_ascii=False),
                    flush=True,
                )
                await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))
                continue

            args.date = run_date
            status_payload = {"scheduler": "daily-market", "date": run_date}
            write_monitor_status("running-auto-paper", **status_payload)
            print(json.dumps({"status": "running-auto-paper", **status_payload}, ensure_ascii=False), flush=True)
            summary = await run_auto_paper(args)
            mark_scheduler_completed(run_date, summary.get("summaryPath"))
            write_monitor_status(
                "completed",
                scheduler="daily-market",
                date=run_date,
                summaryPath=summary.get("summaryPath"),
            )
            print(json.dumps({"results": [summary]}, ensure_ascii=False, indent=2), flush=True)
            continue

        next_open = next_regular_market_open(
            now,
            open_text=args.market_open,
            close_text=args.market_close,
            holidays=holidays,
        )
        wait_seconds = max(1, int((next_open - now).total_seconds()))
        status_payload = {
            "scheduler": "daily-market",
            "now": now.isoformat(timespec="seconds"),
            "nextRunAfter": next_open.isoformat(timespec="seconds"),
        }
        write_monitor_status("waiting-for-market-open", **status_payload)
        print(json.dumps({"status": "waiting-for-market-open", **status_payload}, ensure_ascii=False), flush=True)
        await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))


async def main_async() -> int:
    args = make_parser().parse_args()
    results: list[dict[str, Any]] = []
    monitor_server = None
    if args.monitor:
        monitor_server = start_monitor_server(args.monitor_host, args.monitor_port)
        monitor_url = getattr(monitor_server, "monitor_url", f"http://{args.monitor_host}:{args.monitor_port}")
        print(
            json.dumps(
                {
                    "monitor": "started",
                    "url": monitor_url,
                    "externalDeviceHint": (
                        "Install and log in to Tailscale on your phone, then open this URL."
                        if args.monitor_host.strip().lower() == "tailscale"
                        else "Use http://<Mac-mini-LAN-IP>:%s when bound to 0.0.0.0." % args.monitor_port
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # Recommended OpenAI setup:
    #   Put OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, and Toss keys
    #   in /Users/leejang2/Project/.env, then run:
    #   python main.py
    #   python main.py --provider openai --model gpt-5.5
    #
    # LOCAL LLM ROLLBACK NOTE:
    # Local Ollama/hybrid was disabled because it became the paper-trading
    # bottleneck. To restore it, change the --provider choices back to
    # ["openai", "ollama", "hybrid"], restore resolve_models()' hybrid/ollama
    # branches above, and re-enable the hybrid config block in make_config().
    # Previous example:
    #   python main.py --scenario auto-paper --provider hybrid \
    #     --quick-model qwen3:8b --deep-model gpt-5.5

    if args.scenario == "auto-paper":
        if args.run_mode == "daily-market":
            return await run_daily_market_scheduler(args)
        if not args.date:
            args.date = today_korea().isoformat()
        results.append(await run_auto_paper(args))
        write_monitor_status(
            "completed",
            scheduler="once",
            date=args.date,
            summaryPath=results[-1].get("summaryPath"),
        )
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        if monitor_server:
            monitor_server.shutdown()
        return 0

    if not args.date:
        args.date = today_korea().isoformat()

    if args.scenario in {"all-safe", "tests"} and not args.skip_tests:
        test_result = run_focused_tests()
        results.append(test_result)
        if not test_result["ok"]:
            print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
            return test_result["returnCode"] or 1

    if args.scenario in {"all-safe", "minimal"}:
        results.append(run_minimal(args))

    if args.scenario in {"all-safe", "korea-dry-run"}:
        results.append(await run_korea_toss(args, execute=False))

    if args.scenario == "korea-execute":
        results.append(await run_korea_toss(args, execute=True))

    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
