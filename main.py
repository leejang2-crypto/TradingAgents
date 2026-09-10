from __future__ import annotations

import argparse
import asyncio
import fcntl
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Resolve the shared credentials even when main.py is launched by absolute path
# from a different working directory. Existing exported variables still win.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.agents.utils.experience import ExperienceStore
from tradingagents.dataflows.naver_news import (
    collect_candidate_news_naver,
    collect_news_naver,
    rank_candidate_tickers,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.routing import AGENT_LABELS, DEEP_AGENTS, LLM_DEFAULTS, NEWS_ONLY_ROUTING, describe_routing


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
AUTO_SCENARIOS = {"auto-paper", "auto-live"}
SETTINGS_LOCK = threading.RLock()
SETTING_KEYS = {
    "paper_capital",
    "position_pct",
    "daily_target_return",
    "trading_style",
    "candidate_limit",
    "trade_candidates",
    "candidate_days",
    "candidate_categories",
    "extra_queries",
    "news_display",
    "market_open",
    "market_close",
    "holiday_dates",
    "llm_timeout",
    *LLM_DEFAULTS,
}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "TradingAgents integrated runner. By default it discovers Korean "
            "stock candidates and runs paper trading only."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["auto-paper", "auto-live", "all-safe", "tests", "minimal", "korea-dry-run", "korea-execute"],
        default="auto-paper",
        help=(
            "Execution scenario. auto-paper discovers candidates and records "
            "paper trades only; auto-live uses the same pipeline with Toss orders and requires --yes."
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
        choices=["openai", "ollama", "hybrid"],
        default="openai",
        help=(
            "LLM provider. hybrid defaults to local news analysis and OpenAI for other roles."
        ),
    )
    parser.add_argument("--model", help="Use one model for both quick/deep agents.")
    parser.add_argument("--quick-model", help="Override quick analyst/debate model.")
    parser.add_argument("--deep-model", help="Override deeper manager/portfolio model.")
    for key, value in LLM_DEFAULTS.items():
        if key not in {"provider", "hybrid_agent_providers"}:
            parser.add_argument("--" + key.replace("_", "-"), default=value)
    parser.add_argument(
        "--backend-url",
        help="Optional backend URL. Ollama default: http://localhost:11434/v1.",
    )
    parser.add_argument("--language", default="Korean")
    parser.add_argument(
        "--paper-capital",
        default=DEFAULT_PAPER_CAPITAL,
        help="Configured trading capital in KRW for both automatic modes. Default: 10,000,000.",
    )
    parser.add_argument(
        "--position-pct",
        default=DEFAULT_POSITION_PCT,
        help="Per-candidate BUY budget as a fraction of paper capital. Default: 0.10.",
    )
    parser.add_argument(
        "--daily-target-return",
        default=DEFAULT_DAILY_TARGET_RETURN,
        help="Daily trading target return. Default: 0.01, i.e. 1%%.",
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
            "Automatic trading run mode. daily-market keeps main.py alive and runs "
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
        help="Start a web dashboard for status, paper-trading artifacts, and token-protected settings.",
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
        help="Explicit live-order authorization for auto-live or korea-execute.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="With --scenario all-safe, skip the focused pytest suite.",
    )
    return parser


def resolve_models(args: argparse.Namespace) -> tuple[str, str, str | None]:
    # In hybrid mode quick/deep aliases describe the OpenAI pool. Local models
    # have separate fields so switching providers never reuses an OpenAI ID locally.
    pool = "ollama" if args.provider == "ollama" else "openai"
    quick_model = args.quick_model or args.model or getattr(args, f"{pool}_quick_model", LLM_DEFAULTS[f"{pool}_quick_model"])
    deep_model = args.deep_model or args.model or getattr(args, f"{pool}_deep_model", LLM_DEFAULTS[f"{pool}_deep_model"])
    backend = args.backend_url
    if args.provider == "ollama":
        backend = backend or getattr(args, "ollama_base_url", LLM_DEFAULTS["ollama_base_url"])
    return quick_model, deep_model, backend


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


def auto_scenario(args: argparse.Namespace) -> str:
    return "auto-live" if getattr(args, "scenario", "auto-paper") == "auto-live" else "auto-paper"


def scheduler_state_path(scenario: str = "auto-paper") -> Path:
    if scenario not in AUTO_SCENARIOS:
        raise ValueError("Unknown automatic trading scenario.")
    return LOCAL_DIR / "scheduler" / f"{scenario.replace('-', '_')}_daily_runs.json"


def monitor_status_path() -> Path:
    return LOCAL_DIR / "monitor" / "main_status.json"


def settings_path() -> Path:
    return LOCAL_DIR / "config" / "trading_settings.json"


def monitor_token_path() -> Path:
    return LOCAL_DIR / "monitor" / "monitor_token.txt"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_scheduler_state(scenario: str = "auto-paper") -> dict[str, Any]:
    path = scheduler_state_path(scenario)
    if not path.exists():
        return {"completedDates": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("completedDates"), list):
            raise ValueError("Invalid scheduler state")
        return state
    except (OSError, ValueError) as exc:
        # Do not silently forget completed days and repeat orders after corruption.
        raise ValueError(f"Cannot read scheduler state: {path}") from exc


def mark_scheduler_completed(run_date: str, summary_path: str | None, status: str = "completed", *, scenario: str = "auto-paper") -> None:
    path = scheduler_state_path(scenario)
    state = load_scheduler_state(scenario)
    completed = set(state.get("completedDates", []))
    completed.add(run_date)
    state["completedDates"] = sorted(completed)
    state.setdefault("runs", {})[run_date] = {
        **state.get("runs", {}).get(run_date, {}),
        "completedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        "summaryPath": summary_path,
        "status": status,
    }
    atomic_write_json(path, state)


def has_scheduler_completed(run_date: str, scenario: str = "auto-paper") -> bool:
    state = load_scheduler_state(scenario)
    return run_date in set(state.get("completedDates", [])) or (
        scenario == "auto-live" and run_date in state.get("runs", {})
    )


def mark_live_run_started(run_date: str) -> None:
    state = load_scheduler_state("auto-live")
    state.setdefault("runs", {})[run_date] = {
        "status": "started", "startedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
    }
    # Persist before any broker call. An interrupted run is not replayed on restart.
    atomic_write_json(scheduler_state_path("auto-live"), state)


def default_settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **{key: deepcopy(getattr(args, key, value)) for key, value in LLM_DEFAULTS.items()},
        "paper_capital": args.paper_capital,
        "position_pct": args.position_pct,
        "daily_target_return": args.daily_target_return,
        "trading_style": args.trading_style,
        "candidate_limit": args.candidate_limit,
        "trade_candidates": args.trade_candidates,
        "candidate_days": args.candidate_days,
        "candidate_categories": args.candidate_categories,
        "extra_queries": args.extra_queries,
        "news_display": args.news_display,
        "market_open": args.market_open,
        "market_close": args.market_close,
        "holiday_dates": args.holiday_dates,
        "llm_timeout": args.llm_timeout,
    }


def load_trading_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Settings must be an object")
        return validate_trading_settings(payload)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid settings in {path}: {exc}") from exc


def positive_decimal(key: str, value: Any) -> Decimal:
    try:
        numeric = Decimal(str(value))
        if not numeric.is_finite() or numeric <= 0 or numeric > Decimal("1e15"):
            raise ValueError(f"{key} must be finite and greater than 0 (maximum 1e15).")
        return numeric
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be a valid number.") from exc


def validate_trading_settings(settings: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in settings.items():
        if key not in SETTING_KEYS:
            continue
        if value is None:
            raise ValueError(f"{key} cannot be null.")
        if isinstance(value, str):
            value = value.strip()
        if key in {"paper_capital"}:
            numeric = positive_decimal(key, value)
            if numeric != numeric.to_integral_value():
                raise ValueError("paper_capital must be a whole KRW amount.")
            cleaned[key] = str(numeric.quantize(Decimal("1")))
        elif key in {"position_pct", "daily_target_return"}:
            numeric = positive_decimal(key, value)
            if key == "position_pct" and numeric > 1:
                raise ValueError("position_pct must be 1.0 or less.")
            cleaned[key] = str(numeric)
        elif key in {"candidate_limit", "trade_candidates", "candidate_days", "news_display"}:
            if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
                raise ValueError(f"{key} must be a whole number.")
            numeric_int = int(value)
            if numeric_int < 0:
                raise ValueError(f"{key} must be 0 or greater.")
            if key in {"candidate_limit", "candidate_days", "news_display"} and numeric_int == 0:
                raise ValueError(f"{key} must be greater than 0.")
            if numeric_int > {"candidate_limit": 100, "trade_candidates": 100, "candidate_days": 365, "news_display": 100}[key]:
                raise ValueError(f"{key} exceeds its allowed maximum.")
            cleaned[key] = numeric_int
        elif key in {"market_open", "market_close"}:
            parse_hhmm(str(value))
            cleaned[key] = str(value)
        elif key == "holiday_dates":
            holiday_dates(str(value))
            cleaned[key] = str(value)
        elif key == "llm_timeout":
            numeric = positive_decimal(key, value)
            if numeric > 3600:
                raise ValueError("llm_timeout must be 3600 seconds or less.")
            cleaned[key] = str(numeric)
        elif key == "provider":
            if not isinstance(value, str) or value not in {"openai", "ollama", "hybrid"}:
                raise ValueError("provider must be openai, ollama, or hybrid.")
            cleaned[key] = value
        elif key == "hybrid_agent_providers":
            if not isinstance(value, dict) or any(name not in AGENT_LABELS or not isinstance(provider, str) or provider not in {"openai", "ollama"} for name, provider in value.items()):
                raise ValueError("Invalid agent provider mapping.")
            cleaned[key] = {**NEWS_ONLY_ROUTING, **value}
        elif key.endswith("_model"):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
                raise ValueError(f"{key} must be a model ID.")
            cleaned[key] = value
        elif key == "ollama_base_url":
            parsed = urlparse(str(value))
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1":
                raise ValueError("Ollama URL must be a local http://localhost:11434/v1 address.")
            if parsed.port is None or not 1 <= parsed.port <= 65535:
                raise ValueError("Ollama URL needs a valid port.")
            cleaned[key] = str(value).rstrip("/")
        else:
            cleaned[key] = str(value)
    if "market_open" in cleaned or "market_close" in cleaned:
        open_text = str(cleaned.get("market_open", DEFAULT_MARKET_OPEN))
        close_text = str(cleaned.get("market_close", DEFAULT_MARKET_CLOSE))
        market_window_for(today_korea(), open_text, close_text)
    return cleaned


def save_trading_settings(settings: dict[str, Any]) -> dict[str, Any]:
    with SETTINGS_LOCK:
        existing = load_trading_settings()
        submitted = {key: value for key, value in settings.items() if key in SETTING_KEYS}
        if isinstance(submitted.get("hybrid_agent_providers"), dict):
            submitted["hybrid_agent_providers"] = {**existing.get("hybrid_agent_providers", NEWS_ONLY_ROUTING), **submitted["hybrid_agent_providers"]}
        existing.update(submitted)
        existing = validate_trading_settings(existing)
        atomic_write_json(settings_path(), existing)
        return existing


def initialize_trading_settings(args: argparse.Namespace) -> dict[str, Any]:
    # Explicit CLI flags win at startup; web updates win at the next run.
    defaults = default_settings(args)
    existing = load_trading_settings()
    merged = {**defaults, **existing}
    for key in getattr(args, "_explicit_settings", set()):
        if key in defaults:
            merged[key] = defaults[key]
    pool = "ollama" if merged["provider"] == "ollama" else "openai"
    for kind in ("quick", "deep"):
        override = getattr(args, f"{kind}_model", None) or getattr(args, "model", None)
        if override:
            merged[f"{pool}_{kind}_model"] = override
    saved = save_trading_settings(merged)
    for key in ("model", "quick_model", "deep_model"):
        setattr(args, key, None)
    apply_trading_settings(args)
    return saved


def apply_trading_settings(args: argparse.Namespace) -> argparse.Namespace:
    for key, value in load_trading_settings().items():
        setattr(args, key, value)
    return args


def ensure_monitor_token() -> str:
    with SETTINGS_LOCK:
        return _ensure_monitor_token()


def _ensure_monitor_token() -> str:
    env_token = os.environ.get("TRADINGAGENTS_MONITOR_TOKEN", "").strip()
    if env_token:
        return env_token
    path = monitor_token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            path.chmod(0o600)
            return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def is_authorized_monitor_request(headers: Any) -> bool:
    token = ensure_monitor_token()
    provided = headers.get("X-Monitor-Token", "")
    if not provided:
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            provided = authorization.removeprefix("Bearer ").strip()
    return secrets.compare_digest(provided.encode("utf-8"), token.encode("utf-8"))


def write_monitor_status(status: str, **extra: Any) -> None:
    payload = {
        "status": status,
        "updatedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        **extra,
    }
    path = monitor_status_path()
    atomic_write_json(path, payload)


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
    runtime = read_json_file(monitor_status_path()) or {}
    live = runtime.get("executionMode") == "live"
    scenario = "auto-live" if live else "auto-paper"
    prefix = scenario.replace("-", "_")
    latest_summary_path = latest_file(LOCAL_DIR / prefix, f"{prefix}_*.json")
    latest_candidate_path = latest_file(LOCAL_DIR / "candidates", f"{prefix}_candidates_*.json")
    latest_summary = read_json_file(latest_summary_path) if latest_summary_path else None
    latest_candidates = read_json_file(latest_candidate_path) if latest_candidate_path else None
    trades = read_tail_jsonl(Path(os.environ.get("TRADINGAGENTS_PAPER_TRADE_LOG", str(LOCAL_DIR / "paper_trades" / "trades.jsonl"))), limit=30)
    return {
        "now": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "runtime": runtime,
        "executionMode": "live" if live else "dry-run",
        "scheduler": read_json_file(scheduler_state_path(scenario)) or {"completedDates": []},
        "settings": {**deepcopy(LLM_DEFAULTS), **load_trading_settings()},
        "settingsPath": str(settings_path()),
        "settingsRequireToken": True,
        "latestSummaryPath": str(latest_summary_path) if latest_summary_path else None,
        "latestSummary": latest_summary,
        "latestCandidatePath": str(latest_candidate_path) if latest_candidate_path else None,
        "latestCandidates": latest_candidates,
        "recentTrades": trades,
        "recentLiveOrders": read_tail_jsonl(LOCAL_DIR / "live_orders" / "orders.jsonl", limit=30),
    }


def _compact_text(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def render_monitor_html(snapshot: dict[str, Any]) -> str:
    runtime = snapshot.get("runtime") or {}
    summary = snapshot.get("latestSummary") if isinstance(snapshot.get("latestSummary"), dict) else {}
    candidates = snapshot.get("latestCandidates") if isinstance(snapshot.get("latestCandidates"), dict) else {}
    settings = snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {}
    settings = {**LLM_DEFAULTS, **settings}

    def options(values: dict[str, str], selected: str) -> str:
        return "".join(f'<option value="{_compact_text(value)}"{" selected" if value == selected else ""}>{_compact_text(label)}</option>' for value, label in values.items())

    role_controls = "".join(
        f'<label>{label}{" (현재 미사용)" if name in {"sentiment_analyst", "fundamentals_analyst"} else ""}'
        f' · {"deep" if name in DEEP_AGENTS else "quick"}'
        f'<select name="route_{name}">{options({"openai": "OpenAI", "ollama": "Ollama"}, settings["hybrid_agent_providers"].get(name, "openai"))}</select></label>'
        for name, label in AGENT_LABELS.items()
    )
    model_controls = "".join(
        f'<label>{label}<input name="{name}" list="{"ollama-models" if name.startswith("ollama") else "openai-models"}" required value="{_compact_text(settings[name])}"></label>'
        for name, label in {"openai_quick_model": "OpenAI quick 모델", "openai_deep_model": "OpenAI deep 모델", "ollama_quick_model": "Ollama quick 모델", "ollama_deep_model": "Ollama deep 모델"}.items()
    )
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
    live = snapshot.get("executionMode") == "live"
    trade_rows = []
    for trade in reversed(snapshot.get("recentLiveOrders" if live else "recentTrades") or []):
        plan = trade.get("plan") or {}
        trade_rows.append(
            "<tr>"
            f"<td>{_compact_text(trade.get('recordedAt'))}</td>"
            f"<td>{_compact_text(plan.get('symbol'))}</td>"
            f"<td>{_compact_text(plan.get('action'))}</td>"
            f"<td>{_compact_text(plan.get('order_amount') or plan.get('quantity'))}</td>"
            f"<td>{_compact_text(plan.get('rating'))}</td>"
            f"<td>{_compact_text(trade.get('status'))}{' (체결 미검증)' if not trade.get('fillVerified') else ''}</td>"
            "</tr>"
        )
    analysis_rows = []
    for analysis in (summary.get("analyses") or []):
        toss = analysis.get("toss") or {}
        plan = toss.get("plan") or {}
        order_label = str(plan.get("action") or "-")
        if toss.get("skipped"):
            order_label += " (미기록: " + str(toss["skipped"]) + ")"
        analysis_rows.append(
            "<tr>"
            f"<td>{_compact_text(analysis.get('ticker'))}</td>"
            f"<td>{_compact_text(analysis.get('candidateRank'))}</td>"
            f"<td>{_compact_text(analysis.get('error') or analysis.get('decision'))}</td>"
            f"<td>{_compact_text(order_label)}</td>"
            f"<td>{_compact_text(analysis.get('reportPath'))}</td>"
            f"<td>{'<br>'.join(_compact_text(AGENT_LABELS.get(name, name) + ': ' + route['provider'] + ' / ' + route['model']) for name, route in (analysis.get('agentRouting') or {}).items() if name not in {'sentiment_analyst', 'fundamentals_analyst'}) or _compact_text(analysis.get('provider'))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TradingAgents Monitor</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f8fa; color: #1d2433; }}
    header {{ background: #12343b; color: white; padding: 22px clamp(16px, 4vw, 44px); }}
    main {{ padding: 22px clamp(16px, 4vw, 44px); display: grid; gap: 18px; }}
    section {{ border-bottom: 1px solid #d9dee7; padding: 16px 0; min-width: 0; overflow-x: auto; }}
    h1 {{ margin: 0; font-size: 24px; }}
    h2 {{ margin: 0 0 12px; font-size: 17px; }}
    h3 {{ margin: 18px 0 8px; font-size: 15px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric {{ border-left: 4px solid #2f7d6d; background: #f2fbf8; padding: 10px 12px; }}
    .label {{ color: #5a667a; font-size: 12px; }}
    .value {{ margin-top: 4px; font-size: 18px; font-weight: 700; word-break: break-word; }}
    form {{ display: grid; gap: 14px; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
    label {{ display: grid; gap: 5px; color: #4c5a70; font-size: 14px; }}
    input, textarea, select {{ width: 100%; min-width: 0; box-sizing: border-box; border: 1px solid #c9d0dc; border-radius: 6px; padding: 10px; font: inherit; color: #1d2433; background: #ffffff; }}
    [hidden] {{ display: none !important; }}
    td {{ max-width: 320px; overflow-wrap: anywhere; }}
    textarea {{ min-height: 74px; resize: vertical; }}
    button {{ justify-self: start; border: 0; border-radius: 6px; padding: 10px 14px; background: #12343b; color: white; font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: 0.55; cursor: wait; }}
    .message {{ min-height: 20px; color: #2f7d6d; font-size: 14px; }}
    .muted {{ color: #6b778c; font-size: 13px; }}
    table {{ width: 100%; min-width: 760px; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e9f0; padding: 9px; text-align: left; vertical-align: top; }}
    th {{ color: #4c5a70; background: #f7f8fa; }}
    code {{ color: #40516d; word-break: break-all; }}
  </style>
  <script>
    async function saveSettings(event) {{
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector('button[type="submit"]');
      const message = document.getElementById("settings-message");
      const token = document.getElementById("settings-token").value;
      const payload = Object.fromEntries(new FormData(form).entries());
      delete payload.monitor_token;
      payload.hybrid_agent_providers = {{}};
      for (const key of Object.keys(payload)) {{
        if (key.startsWith("route_")) {{
          payload.hybrid_agent_providers[key.slice(6)] = payload[key];
          delete payload[key];
        }}
      }}
      button.disabled = true;
      message.textContent = "저장 중...";
      try {{
        const response = await fetch("/api/settings", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "X-Monitor-Token": token
          }},
          body: JSON.stringify(payload)
        }});
        const data = await response.json();
        if (!response.ok) {{
          throw new Error(data.error || "설정 저장 실패");
        }}
        message.textContent = "저장됐습니다. 다음 자동 실행부터 반영됩니다.";
      }} catch (error) {{
        message.textContent = "저장 실패: " + error.message;
      }} finally {{
        button.disabled = false;
      }}
    }}
    function updateProvider() {{
      document.getElementById("agent-routing").hidden = document.querySelector('[name="provider"]').value !== "hybrid";
    }}
    function newsLocalPreset() {{
      document.querySelector('[name="provider"]').value = "hybrid";
      document.querySelectorAll('[name^="route_"]').forEach(el => {{ el.value = el.name === "route_news_analyst" ? "ollama" : "openai"; }});
      updateProvider();
    }}
    async function loadLocalModels() {{
      const status = document.getElementById("ollama-status");
      status.textContent = "연결 확인 중...";
      try {{
        const url = document.querySelector('[name="ollama_base_url"]').value;
        const response = await fetch("/api/llm-models?base_url=" + encodeURIComponent(url));
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "Ollama 연결 실패");
        const list = document.getElementById("ollama-models");
        list.replaceChildren(...data.models.map(name => {{ const option = document.createElement("option"); option.value = name; return option; }}));
        status.textContent = data.models.length ? "연결됨 · " + data.models.join(", ") : "연결됨 · 설치 모델 없음";
      }} catch (error) {{ status.textContent = error.message; }}
    }}
    async function refreshStatus() {{
      try {{
        const response = await fetch("/", {{cache: "no-store"}});
        if (!response.ok) throw new Error();
        const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
        document.querySelectorAll("[data-live]").forEach(panel => {{
          const replacement = fresh.querySelector('[data-live="' + panel.dataset.live + '"]');
          if (replacement) panel.replaceWith(replacement);
        }});
        document.getElementById("connection-status").textContent = "연결됨";
      }} catch (error) {{ document.getElementById("connection-status").textContent = "서버 연결 끊김"; }}
    }}
    document.addEventListener("DOMContentLoaded", () => {{ updateProvider(); loadLocalModels(); setInterval(refreshStatus, 30000); }});
  </script>
</head>
<body>
  <header>
    <h1>TradingAgents Monitor</h1>
    <div data-live="timestamp">Asia/Seoul {_compact_text(snapshot.get("now"))}</div>
    <div id="connection-status" role="status">연결됨</div>
  </header>
  <main>
    <section data-live="runtime">
      <h2>Runtime</h2>
      <div class="grid">
        <div class="metric"><div class="label">주문 모드</div><div class="value">{'실투자 · Toss 실제 주문' if live else '모의투자 · 주문 의도 기록'}</div></div>
        <div class="metric"><div class="label">상태</div><div class="value">{_compact_text(runtime.get("status"))}</div></div>
        <div class="metric"><div class="label">업데이트</div><div class="value">{_compact_text(runtime.get("updatedAt"))}</div></div>
        <div class="metric"><div class="label">다음 실행</div><div class="value">{_compact_text(runtime.get("nextRunAfter"))}</div></div>
        <div class="metric"><div class="label">오늘 실행일</div><div class="value">{_compact_text(runtime.get("date") or summary.get("date"))}</div></div>
      </div>
    </section>
    <section data-live="objective">
      <h2>Objective</h2>
      <div class="grid">
        <div class="metric"><div class="label">자본금</div><div class="value">{_compact_text(settings.get("paper_capital") or summary.get("paperCapital"))} KRW</div></div>
        <div class="metric"><div class="label">일 목표 수익률</div><div class="value">{_compact_text(settings.get("daily_target_return") or summary.get("dailyTargetReturn"))}</div></div>
        <div class="metric"><div class="label">종목당 비중</div><div class="value">{_compact_text(settings.get("position_pct") or summary.get("positionPct"))}</div></div>
        <div class="metric"><div class="label">운영 모드</div><div class="value">{_compact_text(summary.get("runMode"))}</div></div>
      </div>
    </section>
    <section>
      <h2>Trading Settings</h2>
      <p class="muted">설정 저장에는 터미널에 출력된 모니터 토큰이 필요합니다. 저장된 값은 다음 자동 실행부터 적용됩니다.</p>
      <form onsubmit="saveSettings(event)">
        <label>모니터 토큰
          <input id="settings-token" name="monitor_token" type="password" autocomplete="current-password" placeholder="터미널의 settings token 입력">
        </label>
        <h3>LLM 설정</h3>
        <div class="form-grid">
          <label>실행 제공자<select name="provider" onchange="updateProvider()">{options({"openai": "OpenAI 전체", "ollama": "Ollama 전체", "hybrid": "역할별 혼합"}, settings["provider"])}</select></label>
          {model_controls}
          <label>Ollama 주소<input name="ollama_base_url" type="url" required value="{_compact_text(settings['ollama_base_url'])}"></label>
        </div>
        <datalist id="ollama-models"></datalist>
        <datalist id="openai-models"><option value="gpt-5.5"><option value="gpt-5.4-mini"></datalist>
        <button type="button" onclick="loadLocalModels()">로컬 모델 목록 확인</button>
        <div id="ollama-status" class="message" role="status"></div>
        <button type="button" onclick="newsLocalPreset()">뉴스만 로컬로 설정</button>
        <div id="agent-routing" class="form-grid">{role_controls}</div>
        <h3>자본과 목표</h3>
        <div class="form-grid">
          <label>자본금 KRW<input name="paper_capital" inputmode="numeric" value="{_compact_text(settings.get("paper_capital", DEFAULT_PAPER_CAPITAL))}"></label>
          <label>종목당 비중<input name="position_pct" inputmode="decimal" value="{_compact_text(settings.get("position_pct", DEFAULT_POSITION_PCT))}"></label>
          <label>일 목표 수익률<input name="daily_target_return" inputmode="decimal" value="{_compact_text(settings.get("daily_target_return", DEFAULT_DAILY_TARGET_RETURN))}"></label>
          <label>전략 스타일<input name="trading_style" value="{_compact_text(settings.get("trading_style", DEFAULT_TRADING_STYLE))}"></label>
        </div>
        <h3>후보 선정</h3>
        <div class="form-grid">
          <label>저장 후보 수<input name="candidate_limit" inputmode="numeric" value="{_compact_text(settings.get("candidate_limit", 20))}"></label>
          <label>분석/매매 후보 수<input name="trade_candidates" inputmode="numeric" value="{_compact_text(settings.get("trade_candidates", 3))}"></label>
          <label>뉴스 조회 일수<input name="candidate_days" inputmode="numeric" value="{_compact_text(settings.get("candidate_days", 3))}"></label>
          <label>뉴스 표시 수<input name="news_display" inputmode="numeric" value="{_compact_text(settings.get("news_display", 5))}"></label>
        </div>
        <label>후보 카테고리
          <input name="candidate_categories" value="{_compact_text(settings.get("candidate_categories", "market,themes,selection_signals,risk_signals"))}">
        </label>
        <label>추가 검색 키워드
          <textarea name="extra_queries">{_compact_text(settings.get("extra_queries", ""), "")}</textarea>
        </label>
        <h3>운영 시간</h3>
        <div class="form-grid">
          <label>장 시작<input name="market_open" value="{_compact_text(settings.get("market_open", DEFAULT_MARKET_OPEN))}"></label>
          <label>장 종료<input name="market_close" value="{_compact_text(settings.get("market_close", DEFAULT_MARKET_CLOSE))}"></label>
          <label>LLM 요청 제한초<input name="llm_timeout" inputmode="numeric" value="{_compact_text(settings.get("llm_timeout", 180))}"></label>
        </div>
        <label>휴장일 YYYY-MM-DD, 쉼표 구분
          <input name="holiday_dates" value="{_compact_text(settings.get("holiday_dates", ""), "")}">
        </label>
        <button type="submit">설정 저장</button>
        <div id="settings-message" class="message" role="status"></div>
      </form>
      <p class="muted"><code>{_compact_text(snapshot.get("settingsPath"))}</code></p>
    </section>
    <section data-live="analyses">
      <h2>Latest Analyses</h2>
      <table><thead><tr><th>Ticker</th><th>Rank</th><th>Decision / Error</th><th>Order</th><th>Report</th><th>사용 LLM</th></tr></thead>
      <tbody>{''.join(analysis_rows) or '<tr><td colspan="6">No analysis yet</td></tr>'}</tbody></table>
      <p><code>{_compact_text(snapshot.get("latestSummaryPath"))}</code></p>
    </section>
    <section data-live="candidates">
      <h2>Candidate Ranking</h2>
      <table><thead><tr><th>Ticker</th><th>Name</th><th>Score</th><th>Mentions</th><th>Positive</th><th>Risk</th></tr></thead>
      <tbody>{''.join(candidate_rows) or '<tr><td colspan="6">No candidates yet</td></tr>'}</tbody></table>
      <p><code>{_compact_text(snapshot.get("latestCandidatePath"))}</code></p>
    </section>
    <section data-live="trades">
      <h2>{'Recent Live Orders' if live else 'Recent Paper Trades'}</h2>
      <table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Amount/Qty</th><th>Rating</th><th>기록 상태</th></tr></thead>
      <tbody>{''.join(trade_rows) or '<tr><td colspan="6">No paper trades yet</td></tr>'}</tbody></table>
    </section>
  </main>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        try:
            self._get()
        except (ValueError, OSError) as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})

    def _get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/llm-models":
            base_url = parse_qs(parsed.query).get("base_url", [LLM_DEFAULTS["ollama_base_url"]])[0]
            try:
                base_url = validate_trading_settings({"ollama_base_url": base_url})["ollama_base_url"]
                with urlopen(base_url.removesuffix("/v1") + "/api/tags", timeout=3) as response:
                    payload = json.load(response)
                models = [row["name"] for row in payload["models"] if isinstance(row.get("name"), str)]
                self._send_json(200, {"ok": True, "models": models})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._send_json(400, {"ok": False, "models": [], "error": f"Ollama 연결/모델 확인 실패: {exc}"})
            return
        if parsed.path == "/api/status":
            self._send_json(200, build_monitor_snapshot())
            return
        if parsed.path == "/api/settings":
            self._send_json(200, {"settings": {**deepcopy(LLM_DEFAULTS), **load_trading_settings()}, "settingsPath": str(settings_path())})
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/settings":
            self.send_error(404)
            return
        if not is_authorized_monitor_request(self.headers):
            self._send_json(401, {"ok": False, "error": "Invalid or missing monitor token."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Invalid Content-Length."})
            return
        if length > 20_000:
            self._send_json(413, {"ok": False, "error": "Settings payload is too large."})
            return
        try:
            raw_body = self.rfile.read(length).decode("utf-8")
            if self.headers.get("Content-Type", "").startswith("application/json"):
                payload = json.loads(raw_body or "{}")
            else:
                parsed_form = parse_qs(raw_body, keep_blank_values=True)
                payload = {key: values[-1] for key, values in parsed_form.items()}
            if not isinstance(payload, dict):
                raise ValueError("Settings payload must be an object.")
            saved = save_trading_settings(payload)
        except (ValueError, TypeError, ArithmeticError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "settings": saved, "settingsPath": str(settings_path())})

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_monitor_server(host: str, port: int, *, scenario: str = "auto-paper") -> ThreadingHTTPServer:
    bind_host, access_host = resolve_monitor_host(host)
    server = ThreadingHTTPServer((bind_host, port), MonitorHandler)
    ensure_monitor_token()
    port = server.server_address[1]
    server.monitor_url = f"http://{access_host}:{port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    write_monitor_status(
        "monitor-started",
        scenario=scenario,
        executionMode="live" if scenario in {"auto-live", "korea-execute"} else "dry-run",
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
            "This is a trading objective, not a guarantee. Agents should "
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
    config = deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "llm_provider": args.provider,
            "deep_think_llm": deep_model,
            "quick_think_llm": quick_model,
            "backend_url": None if args.provider == "hybrid" else backend_url,
            "ollama_backend_url": getattr(args, "ollama_base_url", LLM_DEFAULTS["ollama_base_url"]),
            "output_language": args.language,
            "results_dir": str(LOCAL_DIR / "logs"),
            "data_cache_dir": str(LOCAL_DIR / "cache"),
            "memory_log_path": str(memory_path),
            "checkpoint_enabled": False,
            "llm_timeout": args.llm_timeout,
            "trading_objective": trading_objective(args),
        }
    )
    if args.provider == "hybrid":
        config.update({
            "hybrid_openai_quick_llm": quick_model,
            "hybrid_openai_deep_llm": deep_model,
            "hybrid_ollama_quick_llm": getattr(args, "ollama_quick_model", LLM_DEFAULTS["ollama_quick_model"]),
            "hybrid_ollama_deep_llm": getattr(args, "ollama_deep_model", LLM_DEFAULTS["ollama_deep_model"]),
            "hybrid_agent_providers": deepcopy(getattr(args, "hybrid_agent_providers", NEWS_ONLY_ROUTING)),
            "hybrid_openai_backend_url": backend_url,
        })
    # Local news uses one bounded, tool-free call with already collected data.
    # Keep cloud news's existing tool workflow when OpenAI is selected.
    config["news_local_single_pass"] = describe_routing(config)["news_analyst"]["provider"] == "ollama"
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
        / f"analysis_naver_{safe_ticker_component(args.korea_ticker)}_{args.date}.json"
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
    prefix = auto_scenario(args).replace("-", "_")
    output_prefix = LOCAL_DIR / "candidates" / f"{prefix}_candidates_{args.date}_{stamp}"
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
        "purpose": f"TradingAgents {auto_scenario(args)} candidate discovery",
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
        f"# {auto_scenario(args)} Candidate Discovery",
        "",
        f"- Period: {start_date} to {args.date}",
        f"- Configured capital: {args.paper_capital} KRW",
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
            "실제 Toss 주문은 --scenario auto-live 또는 korea-execute 와 --yes 를 함께 지정해야 실행됩니다."
        )

    quick_model, deep_model, backend_url = resolve_models(args)
    collected_news = collect_korea_news_snapshot(args)
    collected_news_path = collected_news["path"] if collected_news and collected_news.get("ok") else None
    config = make_config(
        args, korea_mode=True, minimal_mode=False, collected_news_path=collected_news_path,
    )
    mode = "live" if execute else "dry-run"
    store = ExperienceStore(LOCAL_DIR / "learning" / "experiences.sqlite3")
    run_id = store.start(
        mode=mode, ticker=args.korea_ticker, trade_date=args.date,
        objective=config["trading_objective"], routing=describe_routing(config),
    )
    # Retrieval learning works with OpenAI, Ollama and hybrid. The legacy shared
    # five-day reflection log is retained in code but not used by trading runs.
    # No weights are trained; at most three earlier experiences per role are
    # added to its existing prompt, with no additional LLM reflection calls.
    config.update({
        "experience_enabled": True,
        "experience_db_path": str(store.path),
        "experience_run_id": run_id,
        "memory_log_path": None,
        "results_dir": str(LOCAL_DIR / "logs" / mode / run_id),
    })
    stage = "ANALYSIS"
    try:
        graph = TradingAgentsGraph(
            selected_analysts=("market", "news"), debug=False, config=config,
        )
        final_state, decision = graph.propagate(args.korea_ticker, args.date)
        store.save_analysis(run_id, final_state)
        report_path = graph.save_reports(final_state, args.korea_ticker)
        stage = "PLANNING"
        plan = plan_from_decision(
            final_state.get("final_trade_decision") or decision,
            args.korea_ticker, max_order_amount=order_budget(args),
        )
        store.event(run_id, "PLANNED", {"plan": plan.__dict__, "reportPath": str(report_path)})
        if args.scenario in AUTO_SCENARIOS and (args.run_mode == "daily-market" or execute) and not is_regular_market_open(
            datetime.now(KOREA_TZ), open_text=args.market_open, close_text=args.market_close,
            holidays=holiday_dates(args.holiday_dates),
        ):
            execution = {"executed": False, "dryRun": not execute, "skipped": "market-closed", "plan": plan.__dict__}
        else:
            stage = "ORDER_ATTEMPT"
            # Persist intent before any external side effect. The broker journal
            # remains authoritative if the process dies before RESULT is saved.
            store.event(run_id, stage, {"plan": plan.__dict__, "fillVerified": False})
            execution = await execute_plan(
                plan, execute=execute,
                live_order_log=LOCAL_DIR / "live_orders" / "orders.jsonl",
                experience_run_id=run_id,
            )
        stage = "RESULT_PERSISTENCE"
        store.event(run_id, "RESULT", {"execution": execution, "fillVerified": False, "realizedPnl": None})
    except BaseException as exc:
        store.event(run_id, "INTERRUPTED" if not isinstance(exc, Exception) else "ERROR", {
            "stage": stage, "error": str(exc), "errorType": type(exc).__name__,
            "fillVerified": False, "realizedPnl": None,
        })
        raise
    return {
        "scenario": "korea-execute" if execute else "korea-dry-run",
        "ticker": args.korea_ticker,
        "date": args.date,
        "provider": args.provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "agentRouting": describe_routing(config),
        "ok": True,
        "decision": decision,
        "reportPath": str(report_path),
        "collectedNews": collected_news,
        "toss": execution,
        "learning": {"enabled": True, "kind": "experience-retrieval", "runId": run_id, "database": str(store.path)},
    }


async def run_auto_paper(args: argparse.Namespace) -> dict[str, Any]:
    return await run_auto_trading(args, execute=False)


async def run_auto_live(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise SystemExit("auto-live requires --yes to authorize actual Toss orders.")
    run_date = today_korea().isoformat()
    if args.date and args.date != run_date:
        raise ValueError("Live trading must use today's Korean market date.")
    args.date = run_date
    if not is_regular_market_open(datetime.now(KOREA_TZ), open_text=args.market_open, close_text=args.market_close, holidays=holiday_dates(args.holiday_dates)):
        raise ValueError("Live trading can only execute during the configured regular session.")
    if has_scheduler_completed(run_date, "auto-live"):
        raise RuntimeError("A live run was already started today; automatic replay is disabled.")
    mark_live_run_started(run_date)
    summary = await run_auto_trading(args, execute=True)
    mark_scheduler_completed(run_date, summary.get("summaryPath"), "completed" if summary["ok"] else "completed-with-errors", scenario="auto-live")
    return summary


async def run_auto_trading(args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    if execute and not args.yes:
        raise SystemExit("Actual Toss orders require --yes.")
    scenario = "auto-live" if execute else "auto-paper"
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
            if (args.run_mode == "daily-market" or execute) and not is_regular_market_open(
                datetime.now(KOREA_TZ), open_text=args.market_open, close_text=args.market_close,
                holidays=holiday_dates(args.holiday_dates),
            ):
                break
            args.korea_ticker = ticker
            candidate_rank = next(
                (index for index, row in enumerate(candidates, start=1) if row["ticker"] == ticker),
                None,
            )
            try:
                result = await run_korea_toss(args, execute=execute)
                result["candidateRank"] = candidate_rank
            except Exception as exc:
                result = {
                    "scenario": "korea-execute" if execute else "korea-dry-run",
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
        "ok": all(row.get("ok", True) for row in analyses) and len(analyses) == len(tickers),
        "scenario": scenario,
        "executionMode": "live" if execute else "dry-run",
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
    prefix = scenario.replace("-", "_")
    output_dir = LOCAL_DIR / prefix
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{prefix}_{args.date}_{datetime.now():%Y%m%d_%H%M%S}.json"
    atomic_write_json(output_path, summary)
    summary["summaryPath"] = str(output_path)
    return summary


async def run_daily_market_scheduler(args: argparse.Namespace) -> int:
    scenario = auto_scenario(args)
    if scenario == "auto-live" and not args.yes:
        raise SystemExit("auto-live requires --yes.")
    mode_status = {"scenario": scenario, "executionMode": "live" if scenario == "auto-live" else "dry-run"}
    apply_trading_settings(args)
    holidays = holiday_dates(args.holiday_dates)
    startup_status = {
        **mode_status,
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
        apply_trading_settings(args)
        holidays = holiday_dates(args.holiday_dates)
        now = datetime.now(KOREA_TZ)
        run_date = now.date().isoformat()
        if is_regular_market_open(
            now,
            open_text=args.market_open,
            close_text=args.market_close,
            holidays=holidays,
        ):
            if has_scheduler_completed(run_date, scenario):
                next_open = next_regular_market_open(
                    now + timedelta(days=1),
                    open_text=args.market_open,
                    close_text=args.market_close,
                    holidays=holidays,
                )
                wait_seconds = max(1, int((next_open - now).total_seconds()))
                status_payload = {
                    **mode_status,
                    "scheduler": "daily-market",
                    "date": run_date,
                    "nextRunAfter": next_open.isoformat(timespec="seconds"),
                }
                outcome = load_scheduler_state(scenario).get("runs", {}).get(run_date, {}).get("status", "completed")
                if outcome == "started":
                    outcome = "interrupted-live-run"
                write_monitor_status("already-completed" if outcome == "completed" else outcome, **status_payload)
                print(
                    json.dumps({"status": "already-completed", **status_payload}, ensure_ascii=False),
                    flush=True,
                )
                await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))
                continue

            args.date = run_date
            status_payload = {**mode_status, "scheduler": "daily-market", "date": run_date}
            write_monitor_status(f"running-{scenario}", **status_payload)
            print(json.dumps({"status": f"running-{scenario}", **status_payload}, ensure_ascii=False), flush=True)
            try:
                summary = await (run_auto_live(args) if scenario == "auto-live" else run_auto_paper(args))
            except Exception as exc:
                summary = {**mode_status, "ok": False, "date": run_date, "error": str(exc)}
                prefix = scenario.replace("-", "_")
                error_path = LOCAL_DIR / prefix / f"{prefix}_{run_date}_failed.json"
                atomic_write_json(error_path, summary)
                summary["summaryPath"] = str(error_path)
            # Record partial failure separately and avoid replaying successful
            # paper orders automatically. A new session starts the next day.
            outcome = "completed" if summary.get("ok", True) else "completed-with-errors"
            mark_scheduler_completed(run_date, summary.get("summaryPath"), outcome, scenario=scenario)
            write_monitor_status(
                outcome,
                **mode_status,
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
            **mode_status,
            "scheduler": "daily-market",
            "now": now.isoformat(timespec="seconds"),
            "nextRunAfter": next_open.isoformat(timespec="seconds"),
        }
        write_monitor_status("waiting-for-market-open", **status_payload)
        print(json.dumps({"status": "waiting-for-market-open", **status_payload}, ensure_ascii=False), flush=True)
        await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))


@contextmanager
def scheduler_process_lock(args: argparse.Namespace):
    if args.scenario not in AUTO_SCENARIOS:
        yield
        return
    path = LOCAL_DIR / "scheduler" / "runner.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another automatic trading process is already running. Stop it before switching modes.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def main_async() -> int:
    args = make_parser().parse_args()
    args._explicit_settings = {
        item.split("=", 1)[0][2:].replace("-", "_") for item in sys.argv[1:] if item.startswith("--")
    }
    with scheduler_process_lock(args):
        return await _main_async(args)


async def _main_async(args: argparse.Namespace) -> int:
    if args.scenario == "auto-live" and not args.yes:
        raise SystemExit("auto-live requires --yes to authorize actual Toss orders.")
    if args.scenario in AUTO_SCENARIOS:
        initialize_trading_settings(args)
    elif args.scenario in {"korea-dry-run", "korea-execute"}:
        # One-shot launchers share web LLM/news settings without rewriting them.
        # Explicit command-line values (including live order limits) still win.
        explicit = getattr(args, "_explicit_settings", set())
        for key, value in load_trading_settings().items():
            if key not in explicit:
                setattr(args, key, value)
    monitor_server = None
    if args.monitor:
        monitor_server = start_monitor_server(args.monitor_host, args.monitor_port, scenario=args.scenario)
        monitor_url = getattr(monitor_server, "monitor_url", f"http://{args.monitor_host}:{args.monitor_port}")
        monitor_token = ensure_monitor_token()
        print(
            json.dumps(
                {
                    "monitor": "started",
                    "url": monitor_url,
                    "settingsTokenPath": str(monitor_token_path()),
                    "settingsTokenPreview": monitor_token[:6] + "...",
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

    try:
        return await run_scenarios(args)
    finally:
        if monitor_server:
            monitor_server.shutdown()
            monitor_server.server_close()


async def run_scenarios(args: argparse.Namespace) -> int:
    results: list[dict[str, Any]] = []
    # Recommended OpenAI setup:
    #   Put OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, and Toss keys
    #   in /Users/leejang2/Project/.env, then run:
    #   python main.py
    #   python main.py --provider openai --model gpt-5.5
    #
    # Local routing is opt-in again. Web settings apply between runs.
    #   python main.py --provider hybrid --ollama-quick-model qwen3:8b
    #   python main.py --provider ollama --ollama-deep-model gemma3:12b
    # Roll back to cloud with --provider openai (no local code removal needed).

    if args.scenario in AUTO_SCENARIOS:
        if args.run_mode == "daily-market":
            return await run_daily_market_scheduler(args)
        if not args.date:
            args.date = today_korea().isoformat()
        results.append(await (run_auto_live(args) if args.scenario == "auto-live" else run_auto_paper(args)))
        write_monitor_status(
            "completed" if results[-1].get("ok", True) else "completed-with-errors",
            scenario=args.scenario,
            executionMode="live" if args.scenario == "auto-live" else "dry-run",
            scheduler="once",
            date=args.date,
            summaryPath=results[-1].get("summaryPath"),
        )
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return 0 if results[-1].get("ok", True) else 1

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
