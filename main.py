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
import sqlite3
import subprocess
import sys
import threading
import tempfile
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Resolve the shared credentials even when main.py is launched by absolute path
# from a different working directory. Existing exported variables still win.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.brokers.account_service import account_view, refresh_account, paper_account, paper_history
from tradingagents.agents.utils.experience import ExperienceStore
from tradingagents.dataflows.naver_news import (
    candidate_symbol_aliases,
    collect_candidate_news_naver,
    collect_news_naver,
    rank_candidate_tickers,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.routing import AGENT_LABELS, DEEP_AGENTS, LLM_DEFAULTS, NEWS_ONLY_ROUTING, describe_routing
from tradingagents.monitoring.auth import DashboardAuth, LoginError
from tradingagents.monitoring.summary import LocalSummaryService, with_report_evidence
from tradingagents.monitoring.control import BusyError, RunController, RunStopped, checkpoint, scheduled_job
from tradingagents.brokers.position_risk import PositionRiskManager, RISK_DEFAULTS, BrokerBackend
from tradingagents.brokers.candidate_screen import screen_candidates
from tradingagents.brokers.intraday import INTRADAY_DEFAULTS, DECIMAL_LIMITS, INTEGER_LIMITS, ENTRY_STRATEGIES
from tradingagents.research.service import ResearchBusy, ResearchService
from tradingagents.research.universe import UniverseError, validate_snapshot as validate_universe


PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = PROJECT_DIR / ".local"
DEFAULT_PAPER_CAPITAL = "10000000"
DEFAULT_POSITION_PCT = "0.10"
DEFAULT_DAILY_TARGET_RETURN = "0.01"
DEFAULT_TRADING_STYLE = "intraday-scalping"
KOREA_TZ = ZoneInfo("Asia/Seoul")
DEFAULT_MARKET_OPEN = "09:00"
DEFAULT_MARKET_CLOSE = "15:30"
AFTERNOON_RUN_TIME = "13:00"
TRADING_SLOTS = {"morning", "afternoon"}
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
    *RISK_DEFAULTS,
    *INTRADAY_DEFAULTS,
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
        default=LLM_DEFAULTS["provider"],
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
        help="Deprecated compatibility option; no longer triggers account-wide profit liquidation. Use --position-target-return.",
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
            "at market open and 13:00 Korea time; once runs immediately and exits."
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
        help="Start a login-protected dashboard for status, accounts, settings, and manual runs.",
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
    for key, value in RISK_DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), type=int, default=value)
    for key, value in INTRADAY_DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), type=int if key in INTEGER_LIMITS else str, default=value,
                            **({"choices": list(ENTRY_STRATEGIES)} if key == "entry_strategy" else {}))
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
        runs = state.setdefault("runs", {})
        if not isinstance(runs, dict):
            raise ValueError("Invalid scheduler runs")
        # Migrate the old once-a-day ledger without replaying a prior order.
        # Unknown legacy timestamps conservatively reserve both slots.
        for run_date in set(runs) | set(state["completedDates"]):
            record = runs.setdefault(run_date, {"status": "completed"})
            if not isinstance(record, dict):
                raise ValueError("Invalid scheduler run")
            if "slots" not in record:
                slots = TRADING_SLOTS
                stamp = record.get("startedAt") or record.get("completedAt")
                if stamp:
                    try:
                        parsed = datetime.fromisoformat(stamp)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=KOREA_TZ)
                        slots = {active_trading_slot(parsed)}
                    except (TypeError, ValueError):
                        pass
                record["slots"] = {slot: dict(record) for slot in slots}
            if not isinstance(record["slots"], dict) or any(
                slot not in TRADING_SLOTS or not isinstance(value, dict) or not isinstance(value.get("status"), str)
                for slot, value in record["slots"].items()
            ):
                raise ValueError("Invalid scheduler slots")
        return state
    except (OSError, ValueError) as exc:
        # Do not silently forget completed days and repeat orders after corruption.
        raise ValueError(f"Cannot read scheduler state: {path}") from exc


def mark_scheduler_completed(run_date: str, summary_path: str | None, status: str = "completed", *, scenario: str = "auto-paper", slot: str = "morning") -> None:
    if slot not in TRADING_SLOTS:
        raise ValueError("Invalid trading slot")
    path = scheduler_state_path(scenario)
    state = load_scheduler_state(scenario)
    completed = set(state.get("completedDates", []))
    completed.add(run_date)
    state["completedDates"] = sorted(completed)
    record = state.setdefault("runs", {}).setdefault(run_date, {"slots": {}})
    completed_run = {
        **record["slots"].get(slot, {}),
        "completedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
        "summaryPath": summary_path,
        "status": status,
    }
    record["slots"][slot] = completed_run
    record.update(completed_run)
    record["lastSlot"] = slot
    atomic_write_json(path, state)


def has_scheduler_completed(run_date: str, scenario: str = "auto-paper", slot: str | None = None) -> bool:
    state = load_scheduler_state(scenario)
    slots = state.get("runs", {}).get(run_date, {}).get("slots", {})
    # A reserved slot is not replayed, including after interruption or error.
    return bool(slots) if slot is None else slot in slots


def mark_run_started(run_date: str, scenario: str, slot: str) -> None:
    if slot not in TRADING_SLOTS:
        raise ValueError("Invalid trading slot")
    state = load_scheduler_state(scenario)
    record = state.setdefault("runs", {}).setdefault(run_date, {"slots": {}})
    if slot in record["slots"]:
        raise RuntimeError("This trading slot was already started; automatic replay is disabled.")
    started = {
        "status": "started", "startedAt": datetime.now(KOREA_TZ).isoformat(timespec="seconds"),
    }
    record["slots"][slot] = started
    record.update(started)
    record["lastSlot"] = slot
    # Persist before any broker call. An interrupted run is not replayed on restart.
    atomic_write_json(scheduler_state_path(scenario), state)


def mark_live_run_started(run_date: str, slot: str = "morning") -> None:
    mark_run_started(run_date, "auto-live", slot)


def active_trading_slot(now: datetime) -> str:
    return "afternoon" if now.astimezone(KOREA_TZ).time() >= parse_hhmm(AFTERNOON_RUN_TIME) else "morning"


def next_trading_slot_time(now: datetime, args: argparse.Namespace, holidays: set[date]) -> datetime:
    afternoon = datetime.combine(now.date(), parse_hhmm(AFTERNOON_RUN_TIME), tzinfo=KOREA_TZ)
    if now < afternoon and parse_hhmm(AFTERNOON_RUN_TIME) < parse_hhmm(args.market_close):
        return afternoon
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=KOREA_TZ)
    return next_regular_market_open(tomorrow, open_text=args.market_open, close_text=args.market_close, holidays=holidays)


def default_settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **{key: getattr(args, key, value) for key, value in RISK_DEFAULTS.items()},
        **{key: getattr(args, key, value) for key, value in INTRADAY_DEFAULTS.items()},
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
        elif key in DECIMAL_LIMITS:
            if isinstance(value, bool):
                raise ValueError(f"{key} must be numeric.")
            try:
                numeric = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"{key} must be numeric.") from exc
            low, high = map(Decimal, DECIMAL_LIMITS[key])
            if not numeric.is_finite() or not low <= numeric <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
            cleaned[key] = str(numeric)
        elif key == "entry_strategy":
            if value not in ENTRY_STRATEGIES:
                raise ValueError("Unknown entry_strategy.")
            cleaned[key] = value
        elif key in RISK_DEFAULTS or key in INTEGER_LIMITS:
            if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
                raise ValueError(f"{key} must be a whole number.")
            limits = INTEGER_LIMITS.get(key, (10, 300) if key == "position_check_seconds" else (5, 120))
            if not limits[0] <= int(value) <= limits[1]:
                raise ValueError(f"{key} must be between {limits[0]} and {limits[1]}.")
            if key == "opening_range_minutes" and int(value) % 5:
                raise ValueError("opening_range_minutes must use 5-minute steps.")
            cleaned[key] = int(value)
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
    if cleaned.get("buy_cutoff_minutes", 30) < cleaned.get("liquidation_minutes", 15):
        raise ValueError("신규 매수 중단은 청산 시작보다 늦을 수 없습니다.")
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


def read_analysis_summary(query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    modes = {"dry-run": "auto_paper", "live": "auto_live"}
    mode = query.get("mode", [""])
    names = query.get("file", [""])
    prefix = modes.get(mode[0])
    if set(query) != {"mode", "file"} or len(mode) != 1 or len(names) != 1 or not prefix:
        return 400, {"ok": False, "error": "분석 요약 파일 요청이 올바르지 않습니다."}
    name = names[0]
    if not re.fullmatch(rf"{prefix}_[A-Za-z0-9_.-]+\.json", name):
        return 400, {"ok": False, "error": "허용된 분석 요약 파일만 열 수 있습니다."}
    directory = LOCAL_DIR / prefix
    path = directory / name
    # Do not follow links out of the generated-summary directory.
    if directory.is_symlink() or path.is_symlink():
        return 403, {"ok": False, "error": "연결된 파일은 열 수 없습니다."}
    try:
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            return 413, {"ok": False, "error": "파일이 너무 큽니다. 화면에서는 2 MB 이하의 요약만 열 수 있습니다."}
        content = json.loads(raw.decode("utf-8"))
        if not isinstance(content, (dict, list)):
            raise ValueError("Invalid summary")
    except FileNotFoundError:
        return 404, {"ok": False, "error": "분석 요약 파일을 찾을 수 없습니다. 목록을 새로고침해 주세요."}
    except (UnicodeDecodeError, ValueError):
        return 422, {"ok": False, "error": "분석 요약 JSON을 읽을 수 없습니다. 저장이 완료된 후 다시 시도해 주세요."}
    except OSError:
        return 503, {"ok": False, "error": "분석 요약 파일을 읽을 수 없습니다. 파일 권한을 확인해 주세요."}
    return 200, {"ok": True, "name": name, "content": content}


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
        "settingsRequireToken": False,
        "authentication": "login-session",
        "latestSummaryPath": str(latest_summary_path) if latest_summary_path else None,
        "latestSummary": latest_summary,
        "latestCandidatePath": str(latest_candidate_path) if latest_candidate_path else None,
        "latestCandidates": latest_candidates,
        "recentTrades": paper_history(LOCAL_DIR, trades),
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
    candidate_universe = candidates.get("universe") or {}
    candidate_scope = (
        f"코스피200 · {len(candidate_universe.get('members', {}))}종목 · 기준일 {candidate_universe.get('asOf')} · {candidate_universe.get('source')}"
        if candidate_universe.get("index") == "KOSPI200" else "구성목록 정보 없음 (이전 결과 또는 등록 필요)"
    )
    settings = snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {}
    settings = {**LLM_DEFAULTS, **INTRADAY_DEFAULTS, **settings}

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
    intraday_controls = "".join(
        f'<label>{label}<input name="{key}" type="number" min="{DECIMAL_LIMITS.get(key, INTEGER_LIMITS.get(key))[0]}" max="{DECIMAL_LIMITS.get(key, INTEGER_LIMITS.get(key))[1]}" step="{"1" if key in INTEGER_LIMITS else "any"}" value="{_compact_text(settings[key])}" required></label>'
        for key, label in {
            "position_target_return": "종목 익절 비율 (0.01 = 1%)",
            "position_stop_loss": "종목 최대 손절 비율 (0.005 = 0.5%)",
            "daily_loss_limit": "일일 손실 한도 (자본 대비 비율)",
            "trade_risk_fraction": "1회 매매 위험 한도 (자본 대비 비율)",
            "estimated_cost_rate": "왕복 비용 추정 비율",
            "min_reward_risk": "최소 비용 반영 손익비",
            "min_relative_volume": "최소 상대 거래량 (배)",
            "min_session_turnover": "최소 당일 추정 거래대금 (원)",
            "max_spread": "최대 호가 스프레드 비율",
            "market_drop_limit": "지수 장초 대비 하락 제한 비율",
            "max_entry_extension": "분봉 신호 대비 가격 이탈 한도",
            "stagnation_minutes": "정체 종목 청산 시간 (분)",
            "max_consecutive_stops": "일일 연속 손절 제한 (회)",
            "opening_range_minutes": "장 초반 범위 (분, 5분 단위)",
            "entry_watch_minutes": "매수 후보 감시 유효시간 (분, 0=감시 안 함)",
        }.items()
    )
    candidate_rows = []
    for row in (candidates.get("candidates") or [])[:10]:
        intraday = row.get("intraday") or {}
        metrics = intraday.get("metrics") or {}
        screen_text = ("통과" if intraday.get("eligible") else "; ".join(intraday.get("reasons", []))) if intraday else "미평가"
        metric_text = (
            f"거래량 {metrics['relativeVolume']:.2f}배 · 거래대금 {metrics['turnoverEstimate'] / 1e8:.1f}억 · "
            f"시장 대비 {metrics['relativeStrength'] * 100:+.2f}%p · ATR {metrics['atrFraction'] * 100:.2f}%"
        ) if metrics else "-"
        if metrics.get("comparisonStart"):
            metric_text += f" · 비교 기준 {metrics['comparisonStart']}"
        candidate_rows.append(
            "<tr>"
            f"<td>{_compact_text(row.get('ticker'))}</td>"
            f"<td>{_compact_text(row.get('name'))}</td>"
            f"<td>{_compact_text(row.get('score'))}</td>"
            f"<td>{_compact_text(row.get('mentionCount'))}</td>"
            f"<td>{_compact_text(row.get('positiveSignalCount'))}</td>"
            f"<td>{_compact_text(row.get('riskSignalCount'))}</td>"
            f"<td>{_compact_text(intraday.get('score'))}</td>"
            f"<td>{_compact_text(screen_text)}<br>{_compact_text(metric_text)}</td>"
            "</tr>"
        )
    live = snapshot.get("executionMode") == "live"
    summary_path = snapshot.get("latestSummaryPath")
    summary_link = '<span class="muted">분석 요약 파일이 아직 없습니다.</span>'
    if summary_path:
        summary_url = "/api/analysis-summary?" + urlencode({
            "mode": "live" if live else "dry-run", "file": Path(summary_path).name,
        })
        summary_link = f'<a href="{_compact_text(summary_url)}" data-summary-link aria-haspopup="dialog"><code>{_compact_text(summary_path)}</code></a>'
    trade_rows = []
    for trade in reversed(snapshot.get("recentLiveOrders" if live else "recentTrades") or []):
        plan = trade.get("plan") or {}
        fill = (trade.get("simulation") or {}) if not live else {}
        if live:
            status = str(trade.get("status")) + (' (체결 미검증)' if not trade.get('fillVerified') else '')
        elif trade.get("ledgerBacked"):
            status = "가상 체결 · 계좌 반영" if fill.get("status") == "SIMULATED_FILLED" else "모의 주문 거절 · 체결 없음"
            if fill.get("reason"):
                status += " · " + str(fill["reason"])
        else:
            status = "주문 계획만 기록 · 계좌 미반영"
        trade_rows.append(
            "<tr>"
            f"<td>{_compact_text(trade.get('recordedAt'))}</td>"
            f"<td>{_compact_text(plan.get('symbol'))}</td>"
            f"<td>{_compact_text(plan.get('action'))}</td>"
            f"<td>{_compact_text(plan.get('order_amount') or plan.get('quantity'))}</td>"
            f"<td>{_compact_text(plan.get('rating'))}</td>"
            f"<td>{_compact_text(status)}</td>"
            + ("" if live else f"<td>{_compact_text(fill.get('quantity'))}</td><td>{_compact_text(fill.get('price'))}</td>"
               f"<td>{_compact_text(fill.get('cashAfter'))}</td><td>{_compact_text(fill.get('quantityAfter'))}</td>") +
            "</tr>"
        )
    candidate_names = {
        row["ticker"]: row.get("name")
        for row in (candidates.get("candidates") or [])
        if row.get("ticker")
    }
    analysis_rows = []
    for analysis in (summary.get("analyses") or []):
        toss = analysis.get("toss") or {}
        plan = toss.get("plan") or {}
        order_label = str(plan.get("action") or "-")
        if toss.get("skipped"):
            order_label += " (미기록: " + str(toss["skipped"]) + ")"
        simulation = toss.get("paperExecution")
        if simulation:
            order_label += " · " + simulation["status"] + (" · " + simulation["reason"] if simulation.get("reason") else "")
        decision_text = str(analysis.get("error") or analysis.get("decision") or "-")
        detail_key = _compact_text(str(analysis.get("ticker")) + ":" + str(analysis.get("reportPath")))
        analysis_rows.append(
            "<tr>"
            f"<td>{_compact_text(analysis.get('ticker'))}</td>"
            f"<td>{_compact_text(analysis.get('name') or candidate_names.get(analysis.get('ticker')))}</td>"
            f"<td>{_compact_text(analysis.get('candidateRank'))}</td>"
            f'<td><details class="analysis-detail" data-detail-key="analysis:{detail_key}"><summary>{_compact_text(decision_text[:100])}{"..." if len(decision_text) > 100 else ""}</summary><div class="analysis-content">{_compact_text(decision_text)}</div></details></td>'
            f"<td>{_compact_text(order_label)}</td>"
            f'<td><details data-detail-key="report:{detail_key}"><summary>보고서 경로</summary><code>{_compact_text(analysis.get("reportPath"))}</code></details></td>'
            f'<td><details data-detail-key="llm:{detail_key}"><summary>{_compact_text(analysis.get("provider"), "사용 LLM")}</summary>'
            f"{'<br>'.join(_compact_text(AGENT_LABELS.get(name, name) + ': ' + route['provider'] + ' / ' + route['model']) for name, route in (analysis.get('agentRouting') or {}).items() if name not in {'sentiment_analyst', 'fundamentals_analyst'}) or _compact_text(analysis.get('provider'))}</details></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TradingAgents Monitor</title>
  <link rel="stylesheet" href="/assets/account.css">
  <link rel="stylesheet" href="/assets/control.css">
  <link rel="stylesheet" href="/assets/dashboard.css">
  <script src="/assets/dashboard.js" defer></script>
  <script src="/assets/research.js" defer></script>
  <script src="/assets/account.js" defer></script>
  <script src="/assets/control.js" defer></script>
  <script src="/assets/positions.js" defer></script>
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
      const submittedSignature = window.Dashboard?.formSignature();
      const payload = Object.fromEntries(new FormData(form).entries());
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
            "X-Requested-With": "TradingAgents"
          }},
          body: JSON.stringify(payload)
        }});
        const data = await response.json();
        if (!response.ok) {{
          throw new Error(data.error || "설정 저장 실패");
        }}
        message.textContent = "저장됐습니다. 다음 자동 실행부터 반영됩니다.";
        window.Dashboard?.saved(submittedSignature);
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
      document.getElementById('settings-form').dispatchEvent(new Event('input', {{bubbles: true}}));
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
        if (response.redirected && new URL(response.url).pathname === "/login") {{ location.assign("/login"); return; }}
        const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
        document.querySelectorAll("[data-live]").forEach(panel => {{
          const replacement = fresh.querySelector('[data-live="' + panel.dataset.live + '"]');
          if (replacement) {{
            if (window.Dashboard) window.Dashboard.replaceLive(panel, replacement);
            else panel.replaceWith(replacement);
          }}
        }});
        document.getElementById("connection-status").textContent = "연결됨";
      }} catch (error) {{ document.getElementById("connection-status").textContent = "서버 연결 끊김"; }}
    }}
    document.addEventListener("DOMContentLoaded", () => {{ updateProvider(); loadLocalModels(); setInterval(refreshStatus, 30000); }});
  </script>
</head>
<body>
  <header id="dashboard-header">
    <div class="header-title">
    <h1>TradingAgents Monitor</h1>
    <button id="logout" type="button">로그아웃</button>
    </div>
    <div class="header-meta">
    <div data-live="header-status"><strong class="mode-badge {'mode-live' if live else 'mode-paper'}">{'실투자 · 실제 주문' if live else '모의투자'}</strong><span>다음 분석: {_compact_text(runtime.get('nextRunAfter'))}</span></div>
    <div data-live="timestamp">Asia/Seoul {_compact_text(snapshot.get("now"))}</div>
    <div id="connection-status" role="status">연결됨</div>
    </div>
    <div id="global-job" role="status">현재 작업 확인 중</div>
    <button id="global-warning" type="button" hidden></button>
  </header>
  <nav id="dashboard-tabs" role="tablist" aria-label="대시보드 메뉴" hidden>
    <button type="button" role="tab" id="tab-overview" aria-controls="view-overview" aria-selected="true" data-view="overview">현황</button>
    <button type="button" role="tab" id="tab-analysis" aria-controls="view-analysis" aria-selected="false" data-view="analysis" tabindex="-1">분석</button>
    <button type="button" role="tab" id="tab-research" aria-controls="view-research" aria-selected="false" data-view="research" tabindex="-1">중장기</button>
    <button type="button" role="tab" id="tab-history" aria-controls="view-history" aria-selected="false" data-view="history" tabindex="-1">거래 내역</button>
    <button type="button" role="tab" id="tab-execution" aria-controls="view-execution" aria-selected="false" data-view="execution" tabindex="-1">실행</button>
    <button type="button" role="tab" id="tab-settings" aria-controls="view-settings" aria-selected="false" data-view="settings" tabindex="-1">설정</button>
  </nav>
  <main id="dashboard-main">
    <section id="research-panel">
      <h2>중장기 종목 탐색</h2>
      <p class="muted">연구 전용 · 자동 주문/보유 편입 없음 · 점수는 예상 수익률이 아닙니다.</p>
      <p id="research-universe-state" role="status">코스피200 구성목록 확인 전</p>
      <p id="research-universe-file-info" style="white-space: pre-line; overflow-wrap: anywhere"></p>
      <p id="research-import-info" role="status"></p>
      <details><summary>KRX 코스피200 구성목록 등록</summary>
        <p><a href="https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd" target="_blank" rel="noopener noreferrer">KRX 데이터 시스템</a></p>
        <form id="research-universe-form">
          <div class="form-grid"><label>구성종목 CSV/TSV<input id="research-universe-file" type="file" accept=".csv,.tsv,text/csv,text/tab-separated-values" required></label>
          <label>CSV 조회 기준일<input name="asOf" type="date" max="{today_korea().isoformat()}" required></label></div>
          <label class="control-check"><input name="confirmed" type="checkbox" required>KRX에서 조회한 코스피200 전체 구성목록임을 확인합니다.</label>
          <label class="control-check"><input name="nonstandardCountConfirmed" type="checkbox">201종목 원본 확인: KRX 원본의 전체 구성종목 수가 201개입니다.</label>
          <button type="submit" id="research-universe-save">구성목록 등록</button>
        </form>
      </details>
      <form id="research-form">
        <div class="form-grid">
          <label>보유 관점<select name="research_horizon"><option value="1-3">1~3개월</option><option value="3-12" selected>3~12개월</option><option value="12-36">1~3년</option></select></label>
          <label>근거 검토 LLM<select name="research_provider"><option value="none">정량 탐색만 (LLM 미사용)</option><option value="openai">OpenAI · API 비용 발생</option><option value="ollama">Ollama · 로컬 자원 사용</option></select></label>
        </div>
        <label>코스피200 내 분석 종목 (비우면 등록된 전체 종목)<textarea name="research_tickers" maxlength="4000" placeholder="005930.KS, 000660.KS, 0126Z0.KS"></textarea></label>
        <details><summary>중장기 선별 조건</summary><div class="form-grid">
          <label>후행 PER 상한 (배)<input name="research_max_pe" type="number" min="1" max="200" step="any" value="25" required></label>
          <label>분기 매출 YoY 하한 초과 (%)<input name="research_min_revenue_growth_pct" type="number" min="-50" max="100" step="any" value="5" required></label>
          <label>부채/자기자본 상한 (%)<input name="research_max_debt_equity" type="number" min="0" max="1000" step="any" value="100" required></label>
          <label>252거래일 최대낙폭 상한 (%)<input name="research_max_drawdown_pct" type="number" min="1" max="100" step="any" value="45" required></label>
          <label>주식 수 증가 상한 · 확인 시 (%)<input name="research_max_dilution_pct" type="number" min="0" max="100" step="any" value="5" required></label>
          <label>20일 평균 거래대금 하한 (KRW)<input name="research_min_turnover_krw" type="number" min="0" max="1000000000000" step="any" value="100000000" required></label>
          <label>추가 검증 점수 하한<input name="research_min_quality_score" type="number" min="0" max="100" step="any" value="60" required></label>
        </div></details>
        <div class="control-actions"><button type="submit" id="research-start">중장기 탐색 실행</button><button type="button" id="research-stop" disabled>탐색 중단</button></div>
      </form>
      <p id="research-state" role="status">탐색 상태 조회 전</p>
      <p id="research-error" role="alert"></p>
      <label>저장된 탐색 결과<select id="research-history"><option value="">최근 결과</option></select></label>
      <div id="research-results" hidden>
        <p id="research-meta"></p>
        <p id="research-llm"></p>
        <p id="research-filter-summary" class="muted"></p>
        <details><summary>평가 기준 및 자료 유의사항</summary><ul id="research-limitations"></ul></details>
        <div class="account-scroll"><table><thead><tr><th>종목</th><th>분류</th><th>종합 / 기존 / 검증 점수</th><th>기존 / 검증 자료 충족률</th><th>가격 / 분기 기준일</th><th>근거와 위험</th></tr></thead><tbody id="research-rows"></tbody></table></div>
        <ul id="research-failures"></ul>
      </div>
    </section>
    <section id="auto-run-panel">
      <h2>자동 매매</h2>
      <div class="auto-run-toolbar">
        <button id="auto-run-once" type="button" data-action="trade" disabled title="저장된 설정으로 후보 선정부터 분석·조건부 매매까지 한 회차 실행">자동실행 (1회)</button>
        <span id="auto-run-mode">실행기 연결 확인 중</span>
      </div>
      <p id="auto-run-state" role="status">실행 상태 확인 중</p>
      <p id="auto-run-message" class="message" role="status"></p>
    </section>
    <section id="manual-panel">
      <h2>수동 실행</h2>
      <div class="control-toolbar">
        <label>종목<input id="manual-ticker" value="005930.KS" maxlength="9" pattern="[0-9]{{6}}\\.(KS|KQ)"></label>
        <label class="control-check"><input id="auto-paused" type="checkbox">자동 실행 일시정지</label>
        <strong id="manual-mode">실행기 연결 확인 중</strong>
      </div>
      <div class="control-actions">
        <button type="button" data-action="news" disabled>종목 뉴스 수집</button>
        <button type="button" data-action="candidates" disabled>후보 종목 선정</button>
        <button type="button" data-action="analyze" disabled>종목 분석만 실행</button>
        <button type="button" data-action="trade" disabled>후보 분석 후 매매</button>
      </div>
      <dl class="control-status"><dt>현재 작업</dt><dd id="current-job">-</dd><dt>대기 작업</dt><dd id="pending-job">-</dd><dt>최근 결과</dt><dd id="last-job">-</dd></dl>
      <button id="cancel-pending" type="button" hidden>대기 실행 취소</button>
      <details id="manual-result" hidden><summary>최근 실행 상세</summary><pre id="manual-result-text"></pre></details>
      <div id="control-message" class="message" role="status"></div>
    </section>
    <dialog id="run-dialog" aria-labelledby="run-dialog-title">
      <h2 id="run-dialog-title">실행 확인</h2>
      <p id="run-dialog-message"></p>
      <p>중단해도 이미 접수된 주문과 체결은 유지됩니다. 새 매매 실행은 추가 주문을 만들 수 있습니다.</p>
      <div class="control-actions">
        <button type="button" id="run-stop">중단 후 실행</button>
        <button type="button" id="run-wait">마무리 후 실행</button>
        <button type="button" id="run-confirm">실제 주문 실행</button>
        <button type="button" id="run-dismiss">취소</button>
      </div>
    </dialog>
    <dialog id="summary-dialog" aria-labelledby="summary-dialog-title">
      <div class="summary-heading"><h2 id="summary-dialog-title">분석 요약</h2>
        <button type="button" id="summary-close" aria-label="분석 요약 닫기" title="닫기" autofocus>×</button></div>
      <p id="summary-file" class="muted"></p>
      <p id="summary-status" role="status"></p>
      <pre id="summary-content" tabindex="0" hidden></pre>
    </dialog>
    <section id="positions-panel">
      <h2>보유 관리 · 청산</h2>
      <p id="positions-status" role="status">보유 관리 연결 확인 중</p>
      <div id="positions-daily"></div>
      <p class="muted">매수일 포함 최대 2거래일. 자동 분석을 일시정지해도 보유 점검·청산은 계속됩니다.</p>
      <div class="account-scroll"><table><thead><tr><th>종목</th><th>관리 수량</th><th>평균 매입가</th><th>종목 수익률</th><th>익절가 / 손절가</th><th>매수일</th><th>최종 보유 한도</th><th>다음 거래일까지 이월 허용</th><th>청산 상태</th></tr></thead><tbody id="positions-body"></tbody></table></div>
      <details data-detail-key="entry-checks"><summary>종목별 진입 검사</summary><div id="entry-checks"></div></details>
      <details data-detail-key="entry-watches" open><summary>매수 조건 대기 후보</summary><div id="entry-watches"></div></details>
      <p id="positions-warning" role="status"></p>
      <details id="position-adoption" hidden><summary>기존 실계좌 보유분 편입</summary>
        <form id="position-adopt-form">
          <div class="form-grid"><label>종목 코드<input name="symbol" pattern="[A-Z0-9]{{6}}" maxlength="6" placeholder="005930" required></label>
          <label>실제 최초 매수일<input name="acquiredDate" type="date" max="{today_korea().isoformat()}" required></label></div>
          <label class="control-check"><input name="confirmed" type="checkbox" required>해당 종목의 현재 보유 전량을 자동 청산 대상으로 편입합니다.</label>
          <button type="submit">보유분 편입</button>
          <p id="position-adopt-message" role="status"></p>
        </form>
      </details>
      <div id="position-order-panel"><h3>최근 주문 체결 확인</h3>
      <div class="account-scroll"><table><thead><tr><th>종목</th><th>구분</th><th>주문 수량/금액</th><th>누적 체결 수량</th><th>상태</th><th>주문 ID</th></tr></thead><tbody id="position-orders"></tbody></table></div>
      </div>
    </section>
    <section data-live="runtime">
      <h2>Runtime</h2>
      <div class="grid">
        <div class="metric"><div class="label">주문 모드</div><div class="value">{'실투자 · Toss 실제 주문' if live else '모의투자 · 가상 체결 계좌'}</div></div>
        <div class="metric"><div class="label">상태</div><div class="value">{_compact_text(runtime.get("status"))}</div></div>
        <div class="metric"><div class="label">업데이트</div><div class="value">{_compact_text(runtime.get("updatedAt"))}</div></div>
        <div class="metric"><div class="label">다음 실행</div><div class="value">{_compact_text(runtime.get("nextRunAfter"))}</div></div>
        <div class="metric"><div class="label">오늘 실행일</div><div class="value">{_compact_text(runtime.get("date") or summary.get("date"))}</div></div>
      </div>
    </section>
    <section id="account-panel">
      <h2>계좌 · 수익률</h2>
      <div class="account-toolbar">
        <fieldset><legend>계좌</legend><div class="account-options">
          <label><input type="radio" name="account_mode" value="dry-run"{' checked' if not live else ''}>모의투자</label>
          <label><input type="radio" name="account_mode" value="live"{' checked' if live else ''}>실투자</label>
        </div></fieldset>
        <fieldset><legend>수익률 단위</legend><div class="account-options">
          <label><input type="radio" name="return_period" value="day" checked>일</label>
          <label><input type="radio" name="return_period" value="week">주</label>
          <label><input type="radio" name="return_period" value="month">월</label>
          <label><input type="radio" name="return_period" value="year">년</label>
        </div></fieldset>
        <label>수익률 조회 기준일<input id="account-date" type="date" value="{today_korea().isoformat()}"></label>
        <button id="account-load" type="button">조회</button>
        <button id="account-refresh" type="button">최신 자료 조회</button>
      </div>
      <div id="account-message" class="message" role="status"></div>
      <div id="account-results" hidden>
        <p id="account-source"></p>
        <div id="account-metrics" class="grid"></div>
        <p id="account-note"></p>
        <h3 id="returns-title">기간별 수익률</h3><div id="account-returns" class="account-scroll"></div>
        <h3>보유주식</h3><div id="account-holdings" class="account-scroll"></div>
        <h3>최근 주문 · 체결</h3><div id="account-orders" class="account-scroll"></div>
      </div>
    </section>
    <section data-live="objective">
      <h2>Objective</h2>
      <div class="grid">
        <div class="metric"><div class="label">자본금</div><div class="value">{_compact_text(settings.get("paper_capital") or summary.get("paperCapital"))} KRW</div></div>
        <div class="metric"><div class="label">종목별 익절 목표</div><div class="value">{Decimal(settings['position_target_return']) * 100:.2f}%</div></div>
        <div class="metric"><div class="label">종목당 비중</div><div class="value">{_compact_text(settings.get("position_pct") or summary.get("positionPct"))}</div></div>
        <div class="metric"><div class="label">운영 모드</div><div class="value">{_compact_text(summary.get("runMode"))}</div></div>
      </div>
    </section>
    <section id="settings-panel">
      <h2>Trading Settings</h2>
      <form id="settings-form" onsubmit="saveSettings(event)">
        <details class="settings-group" data-detail-key="settings-llm"><summary>LLM 설정</summary>
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
        </details>
        <details class="settings-group" data-detail-key="settings-capital"><summary>자본과 목표</summary>
        <div class="form-grid">
          <label>자본금 KRW<input name="paper_capital" inputmode="numeric" value="{_compact_text(settings.get("paper_capital", DEFAULT_PAPER_CAPITAL))}"></label>
          <label>종목당 비중<input name="position_pct" inputmode="decimal" value="{_compact_text(settings.get("position_pct", DEFAULT_POSITION_PCT))}"></label>
          <label>전략 스타일<input name="trading_style" value="{_compact_text(settings.get("trading_style", DEFAULT_TRADING_STYLE))}"></label>
        </div>
        </details>
        <details class="settings-group" data-detail-key="settings-candidates"><summary>후보 선정</summary>
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
        </details>
        <details class="settings-group" data-detail-key="settings-intraday" open><summary>단타 매매 기준</summary>
        <div class="form-grid">
          <label>분봉 진입 전략<select name="entry_strategy">{options(ENTRY_STRATEGIES, settings['entry_strategy'])}</select></label>
          {intraday_controls}
        </div>
        </details>
        <details class="settings-group" data-detail-key="settings-hours"><summary>운영 시간</summary>
        <div class="form-grid">
          <label>보유 점검 주기 (초)<input name="position_check_seconds" type="number" min="10" max="300" value="{settings.get('position_check_seconds', 30)}"></label>
          <label>장 종료 전 청산 시작 (분)<input name="liquidation_minutes" type="number" min="5" max="120" value="{settings.get('liquidation_minutes', 15)}"></label>
          <label>장 종료 전 신규 매수 중단 (분)<input name="buy_cutoff_minutes" type="number" min="5" max="120" value="{settings.get('buy_cutoff_minutes', 30)}"></label>
        </div>
        <div class="form-grid">
          <label>장 시작<input name="market_open" value="{_compact_text(settings.get("market_open", DEFAULT_MARKET_OPEN))}"></label>
          <label>장 종료<input name="market_close" value="{_compact_text(settings.get("market_close", DEFAULT_MARKET_CLOSE))}"></label>
          <label>LLM 요청 제한초<input name="llm_timeout" inputmode="numeric" value="{_compact_text(settings.get("llm_timeout", 180))}"></label>
        </div>
        <label>휴장일 YYYY-MM-DD, 쉼표 구분
          <input name="holiday_dates" value="{_compact_text(settings.get("holiday_dates", ""), "")}">
        </label>
        </details>
        <div class="settings-savebar"><button type="submit">설정 저장</button>
        <span id="settings-dirty" role="status"></span><div id="settings-message" class="message" role="status"></div></div>
      </form>
      <details data-detail-key="settings-path"><summary>설정 파일 경로</summary><p class="muted"><code>{_compact_text(snapshot.get("settingsPath"))}</code></p></details>
    </section>
    <section data-live="analyses">
      <h2>Latest Analyses</h2>
      <table><thead><tr><th>Ticker</th><th>종목명</th><th>Rank</th><th>Decision / Error</th><th>Order</th><th>Report</th><th>사용 LLM</th></tr></thead>
      <tbody>{''.join(analysis_rows) or '<tr><td colspan="7">No analysis yet</td></tr>'}</tbody></table>
      <details data-detail-key="analysis-path"><summary>분석 요약 파일 경로</summary><p>{summary_link}</p></details>
      <div id="local-summary-panel" data-query="{_compact_text(urlencode({'mode': 'live' if live else 'dry-run', 'file': Path(summary_path).name}) if summary_path else '', '')}">
        <h3>핵심 분석 요약 · 로컬 LLM</h3>
        <button type="button" id="local-summary-generate" {'disabled' if not summary_path else ''}>로컬 요약 생성</button>
        <p id="local-summary-status" role="status">{'요약 생성 전' if summary_path else '분석 요약 파일이 아직 없습니다.'}</p>
        <div id="local-summary-result"></div>
      </div>
    </section>
    <section data-live="candidates">
      <h2>Candidate Ranking</h2>
      <p>{_compact_text(candidate_scope)}</p>
      <p class="muted">시장평가: {_compact_text((candidates.get('intradayScreen') or {}).get('status'), '미평가')} · 기준 시각: {_compact_text((candidates.get('intradayScreen') or {}).get('asOf'))}</p>
      <p role="status">{_compact_text(candidates.get('selectionReason') or candidates.get('error'), '')}</p>
      {''.join('<p class="muted">' + _compact_text(warning) + '</p>' for warning in (candidates.get('warnings') or []))}
      <div class="candidate-scroll"><table><thead><tr><th>Ticker</th><th>Name</th><th>뉴스 점수</th><th>Mentions</th><th>Positive</th><th>Risk</th><th>단타 점수</th><th>시장자료 평가</th></tr></thead>
      <tbody>{''.join(candidate_rows) or '<tr><td colspan="8">No candidates yet</td></tr>'}</tbody></table></div>
      <details data-detail-key="candidate-path"><summary>후보 파일 경로</summary><p><code>{_compact_text(snapshot.get("latestCandidatePath"))}</code></p></details>
    </section>
    <section data-live="trades">
      <h2>{'Recent Live Orders' if live else 'Recent Paper Trades'}</h2>
      <table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>주문 금액/수량</th><th>Rating</th><th>기록 상태</th>{'' if live else '<th>체결 수량</th><th>체결가</th><th>처리 후 현금</th><th>처리 후 보유 수량</th>'}</tr></thead>
      <tbody>{''.join(trade_rows) or '<tr><td colspan="' + ('6' if live else '10') + '">No paper trades yet</td></tr>'}</tbody></table>
    </section>
  </main>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        super().end_headers()

    def _authorized(self):
        auth = getattr(self.server, "auth", None)
        return (isinstance(auth, DashboardAuth) and auth.authorized(self.headers)) or is_authorized_monitor_request(self.headers)

    def _browser_write_allowed(self):
        # Explicit API bearer credentials remain compatible; cookies need same-origin JS.
        if is_authorized_monitor_request(self.headers):
            return True
        origin = self.headers.get("Origin")
        return (self.headers.get("X-Requested-With") == "TradingAgents" and
                self.headers.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"} and
                (not origin or (urlparse(origin).scheme in {"http", "https"} and
                                urlparse(origin).netloc == self.headers.get("Host"))))

    def _send_json(self, status_code: int, payload: dict[str, Any], *, cookie=None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        try:
            self._get()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})

    def _get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/assets/account.js", "/assets/account.css", "/assets/control.js", "/assets/control.css", "/assets/login.js", "/assets/positions.js", "/assets/dashboard.js", "/assets/dashboard.css", "/assets/research.js"}:
            name = parsed.path.rsplit("/", 1)[-1]
            payload = (PROJECT_DIR / "tradingagents" / "monitoring" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/login":
            payload = (PROJECT_DIR / "tradingagents/monitoring/login.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self._authorized():
            if parsed.path in {"/", "/index.html"}:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json(401, {"ok": False, "error": "로그인이 필요합니다."})
            return
        if parsed.path == "/api/research":
            research = getattr(self.server, "research", None)
            self._send_json(200, research.snapshot(parse_qs(parsed.query).get("id", [None])[0])
                if isinstance(research, ResearchService) else {"available": False})
            return
        if parsed.path == "/api/control":
            control = getattr(self.server, "control", None)
            self._send_json(200, control.snapshot() if isinstance(control, RunController) else {"available": False})
            return
        if parsed.path == "/api/positions":
            risk = getattr(self.server, "risk", None)
            self._send_json(200, risk.snapshot() if isinstance(risk, PositionRiskManager) else {"enabled": False})
            return
        if parsed.path == "/api/account":
            query = parse_qs(parsed.query)
            self._send_json(200, account_view(LOCAL_DIR,
                query.get("mode", ["dry-run"])[0], query.get("period", ["day"])[0],
                query.get("through", [None])[0], load_trading_settings().get("paper_capital", DEFAULT_PAPER_CAPITAL)))
            return
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
        if parsed.path == "/api/analysis-summary":
            status, payload = read_analysis_summary(parse_qs(parsed.query, keep_blank_values=True))
            self._send_json(status, payload)
            return
        if parsed.path == "/api/analysis-summary/local":
            self._local_summary(parsed.query, start=False)
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
        try:
            self._post()
        except (ValueError, OSError, sqlite3.Error):
            self._send_json(503, {"ok": False, "error": "서버의 인증 또는 저장 파일을 확인하세요."})

    def _post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/positions/watch-cancel", "/api/analysis-summary/local", "/api/settings", "/api/account/refresh", "/api/login", "/api/logout", "/api/control/run", "/api/control/pause", "/api/control/cancel-pending", "/api/positions/carry", "/api/positions/adopt", "/api/research/run", "/api/research/stop", "/api/research/universe"}:
            self.send_error(404)
            return
        if parsed.path != "/api/login" and not self._authorized():
            self._send_json(401, {"ok": False, "error": "로그인이 필요합니다."})
            return
        if not self._browser_write_allowed():
            self._send_json(403, {"ok": False, "error": "다른 출처의 요청은 허용하지 않습니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Invalid Content-Length."})
            return
        if length > (150_000 if parsed.path == "/api/research/universe" else 20_000):
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
            if parsed.path == "/api/analysis-summary/local":
                self._local_summary(parsed.query, start=True)
                return
            if parsed.path.startswith("/api/research/"):
                research = getattr(self.server, "research", None)
                if not isinstance(research, ResearchService):
                    raise ValueError("중장기 탐색 실행기가 없습니다.")
                try:
                    if parsed.path.endswith("/stop"):
                        state = research.stop(payload.get("requestId"))
                    elif parsed.path.endswith("/universe"):
                        state = research.register_universe(payload)
                    else:
                        control = getattr(self.server, "control", None)
                        if isinstance(control, RunController):
                            trading = control.snapshot()
                            if trading.get("active") or trading.get("pending"):
                                raise ResearchBusy("단타 분석/매매가 진행 중이거나 대기 중입니다. 완료 후 탐색을 실행하세요.")
                        state = research.submit(payload, load_trading_settings())
                except ResearchBusy as exc:
                    self._send_json(409, {"ok": False, "error": str(exc)})
                    return
                self._send_json(202, {"ok": True, **state})
                return
            if parsed.path in {"/api/positions/carry", "/api/positions/adopt", "/api/positions/watch-cancel"}:
                risk = getattr(self.server, "risk", None)
                if not isinstance(risk, PositionRiskManager):
                    raise ValueError("보유 관리 실행기가 없습니다.")
                if parsed.path.endswith("/watch-cancel"):
                    if not isinstance(payload.get("symbol"), str) or not isinstance(payload.get("id"), str):
                        raise ValueError("감시 후보와 ID가 필요합니다.")
                    risk.entry_watch.cancel(payload["symbol"], "사용자 감시 취소", expected_id=payload["id"])
                elif parsed.path.endswith("/carry"):
                    risk.carry(payload.get("symbol"), payload.get("cycle"), payload.get("allowed"))
                else:
                    risk.adopt(payload.get("symbol"), payload.get("acquiredDate"), payload.get("confirmed"))
                self._send_json(200, {"ok": True})
                return
            if parsed.path in {"/api/login", "/api/logout"}:
                auth = getattr(self.server, "auth", None)
                if not isinstance(auth, DashboardAuth):
                    raise ValueError("Login service unavailable.")
                if parsed.path == "/api/login":
                    try:
                        token = auth.login(payload.get("username"), payload.get("password"), self.client_address[0])
                    except LoginError as exc:
                        self._send_json(exc.status, {"ok": False, "error": str(exc)})
                        return
                    auth.logout(self.headers)
                    self._send_json(200, {"ok": True}, cookie=auth.cookie(token))
                else:
                    auth.logout(self.headers)
                    self._send_json(200, {"ok": True}, cookie=auth.cookie("", clear=True))
                return
            if parsed.path.startswith("/api/control/"):
                control = getattr(self.server, "control", None)
                if not isinstance(control, RunController):
                    self._send_json(409, {"ok": False, "error": "수동 실행은 daily-market 실행기에서 사용할 수 있습니다."})
                    return
                try:
                    if parsed.path.endswith("/pause"):
                        state = control.pause(payload.get("paused"))
                    elif parsed.path.endswith("/cancel-pending"):
                        state = control.cancel_pending(payload.get("requestId"))
                    else:
                        settings = load_trading_settings()
                        if payload.get("action") == "trade" and not is_regular_market_open(
                            datetime.now(KOREA_TZ), open_text=settings.get("market_open", DEFAULT_MARKET_OPEN),
                            close_text=settings.get("market_close", DEFAULT_MARKET_CLOSE),
                            holidays=holiday_dates(settings.get("holiday_dates", "")),
                        ):
                            raise ValueError("매매 실행은 설정된 정규장 시간에만 가능합니다.")
                        state = control.submit(payload, settings)
                    risk = getattr(self.server, "risk", None)
                    if isinstance(risk, PositionRiskManager) and (state.get("paused") or state.get("stopRequested")):
                        risk.entry_watch.cancel(reason="사용자 일시정지/중단")
                except BusyError as exc:
                    self._send_json(409, {"ok": False, "error": str(exc), "control": control.snapshot()})
                    return
                self._send_json(202, {"ok": True, "control": state})
                return
            if parsed.path == "/api/account/refresh":
                try:
                    refresh_account(LOCAL_DIR, payload.get("mode", "dry-run"), load_trading_settings().get("paper_capital", DEFAULT_PAPER_CAPITAL))
                except Exception as exc:
                    self._send_json(503, {"ok": False, "error": f"계좌 갱신 실패 ({type(exc).__name__}). API 설정과 연결을 확인하세요."})
                    return
                self._send_json(200, {"ok": True})
                return
            saved = save_trading_settings(payload)
            risk = getattr(self.server, "risk", None)
            if isinstance(risk, PositionRiskManager):
                risk.entry_watch.cancel(reason="사용자 설정 저장: 재분석 필요")
        except (ValueError, TypeError, ArithmeticError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "settings": saved, "settingsPath": str(settings_path())})

    def _local_summary(self, query: str, *, start: bool) -> None:
        status, payload = read_analysis_summary(parse_qs(query, keep_blank_values=True))
        if status != 200:
            self._send_json(status, payload)
            return
        service = getattr(self.server, "local_summary", None)
        if not isinstance(service, LocalSummaryService):
            self._send_json(503, {"ok": False, "error": "대시보드 서버를 재시작하세요."})
            return
        content = with_report_evidence(payload["content"], LOCAL_DIR)
        state = service.request(content, load_trading_settings(), start=start)
        self._send_json(202 if state["status"] == "running" else 200, {"ok": True, **state})

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_monitor_server(host: str, port: int, *, scenario: str = "auto-paper", control=None, risk=None) -> ThreadingHTTPServer:
    bind_host, access_host = resolve_monitor_host(host)
    server = ThreadingHTTPServer((bind_host, port), MonitorHandler)
    server.local_summary = LocalSummaryService()
    server.auth = DashboardAuth(LOCAL_DIR / "config/web_login.json",
                               secure_cookie=os.getenv("TRADINGAGENTS_WEB_SECURE_COOKIE", "").lower() in {"1", "true"})
    server.control = control
    server.risk = risk
    server.research = ResearchService(LOCAL_DIR / "long_term", PROJECT_DIR)
    ensure_monitor_token()
    port = server.server_address[1]
    server.monitor_url = f"http://{access_host}:{port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.account_stop = threading.Event()

    def sample_account():
        # Record valuations even when no browser is open. No orders are sent.
        while not server.account_stop.is_set():
            try:
                refresh_account(LOCAL_DIR, "live" if scenario in {"auto-live", "korea-execute"} else "dry-run",
                    load_trading_settings().get("paper_capital", DEFAULT_PAPER_CAPITAL))
            except Exception:
                pass  # The authenticated refresh endpoint reports connection errors.
            server.account_stop.wait(300)

    server.account_thread = threading.Thread(target=sample_account, daemon=True)
    server.account_thread.start()
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
    tailscale_network = ipaddress.ip_network("100.64.0.0/10")

    def valid_address(value: str) -> str | None:
        try:
            candidate = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            return None
        return str(candidate) if candidate in tailscale_network else None

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            if address := valid_address(line.strip()):
                return address

    # Finder-launched macOS terminals may omit /sbin from PATH.
    for executable in ("ifconfig", "/sbin/ifconfig"):
        try:
            result = subprocess.run(
                [executable], text=True, capture_output=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", result.stdout):
            if address := valid_address(match.group(1)):
                return address
    return None


def order_budget(args: argparse.Namespace) -> Decimal:
    if args.max_order_amount:
        return Decimal(args.max_order_amount)
    capital = Decimal(args.paper_capital)
    position_pct = Decimal(args.position_pct)
    return (capital * position_pct).quantize(Decimal("1"))


def daily_profit_target(args: argparse.Namespace) -> Decimal:
    # Legacy report compatibility only; never used as an execution trigger.
    capital = Decimal(args.paper_capital)
    target_return = Decimal(args.daily_target_return)
    return (capital * target_return).quantize(Decimal("1"))


def trading_objective(args: argparse.Namespace) -> dict[str, str]:
    market_close = getattr(args, "market_close", DEFAULT_MARKET_CLOSE)
    close = datetime.strptime(market_close, "%H:%M")
    return {
        "style": args.trading_style,
        "capital_krw": str(Decimal(args.paper_capital).quantize(Decimal("1"))),
        "target_scope": "per-position gross price return; not daily account return",
        "position_target_return": str(Decimal(getattr(args, "position_target_return", "0.01"))),
        "position_stop_loss": str(getattr(args, "position_stop_loss", "0.005")),
        "daily_loss_limit": str(getattr(args, "daily_loss_limit", "0.02")),
        "per_trade_budget_krw": str(order_budget(args)),
        "market_close": market_close,
        "liquidation_start": (close - timedelta(minutes=int(getattr(args, "liquidation_minutes", 15)))).strftime("%H:%M"),
        "buy_cutoff": (close - timedelta(minutes=int(getattr(args, "buy_cutoff_minutes", 30)))).strftime("%H:%M"),
        "overnight_policy": "explicit-approval-only",
        "max_holding_sessions": "2",
        "holding_period": "Same-day liquidation by default. Only explicit user approval permits one overnight hold; liquidate by the second trading session including acquisition day. HOLD never authorizes an extension.",
        "risk_note": (
            "This is a trading objective, not a guarantee. Agents should "
            "prefer liquid Korean stocks with clear intraday catalysts, define "
            "entry/stop/exit levels, and avoid chasing illiquid or already-spiked names. "
            "Only a deterministic completed-5m-bar VWAP/volume/price-action filter may authorize entry. "
            "Gross per-position price target excludes fees, taxes and slippage. No daily profit quota."
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
        / f"analysis_naver_{safe_ticker_component(args.korea_ticker)}_{args.date}_{datetime.now():%H%M%S_%f}.json"
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


def load_intraday_universe() -> dict[str, Any]:
    path = LOCAL_DIR / "long_term" / "universe.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UniverseError("단타 후보용 코스피200 목록이 없습니다. 중장기 탭에서 KRX 전체 구성목록을 등록하세요.") from None
    return validate_universe(snapshot)


def collect_candidate_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    start_date = (
        datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=args.candidate_days)
    ).strftime("%Y-%m-%d")
    categories = split_csv(args.candidate_categories)
    extra_queries = split_csv(args.extra_queries)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = auto_scenario(args).replace("-", "_")
    output_prefix = LOCAL_DIR / "candidates" / f"{prefix}_candidates_{args.date}_{stamp}"
    universe = None
    warnings = []
    try:
        universe = load_intraday_universe()
        aliases = candidate_symbol_aliases(universe["members"])
        korean_name_count = sum(any(re.search(r"[가-힣]", name) for name in names) for names in aliases.values())
        if korean_name_count < len(aliases):
            warnings.append(f"한글명/별칭이 없는 {len(aliases) - korean_name_count}종목은 코드·등록명으로만 뉴스를 인식합니다. 한글 KRX 목록을 사용하면 한글 기사 인식 범위를 넓힐 수 있습니다.")
        articles = collect_candidate_news_naver(
            start_date,
            args.date,
            categories=categories,
            display=args.news_display,
            sort="date",
            extra_queries=extra_queries,
        )
        candidates = rank_candidate_tickers(articles, symbol_map=universe["members"], limit=args.candidate_limit,
                                           as_of=datetime.now(KOREA_TZ))
        ok = True
        error = None
    except RunStopped:
        raise
    except Exception as exc:
        # Discovery failure blocks new entries, not the independent holding monitor.
        articles = []
        candidates = []
        ok = False
        error = str(exc)

    payload = {
        "source": "Naver Search API",
        "purpose": f"TradingAgents {auto_scenario(args)} candidate discovery",
        "ok": ok,
        "error": error,
        "universe": universe,
        "warnings": warnings,
        "selectionReason": error or ("코스피200 뉴스 후보 없음; 신규 후보 분석/주문 생략" if not candidates else None),
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
        f"- Per-position gross target return: {Decimal(getattr(args, 'position_target_return', '.01')):.2%}",
        "- Target scope: each position, before fees/taxes/slippage; no daily profit quota",
        f"- Trading style: {args.trading_style}",
        f"- Article count: {len(articles)}",
        f"- Candidate count: {len(candidates)}",
        f"- Universe: KOSPI200 / {len(universe['members']) if universe else 0} constituents",
        f"- Universe as of: {universe['asOf'] if universe else 'unavailable'}",
        f"- Selection status: {payload['selectionReason'] or 'ranked'}",
        *[f"- Warning: {warning}" for warning in warnings],
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
        "universe": universe,
        "warnings": warnings,
        "selectionReason": payload["selectionReason"],
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
    checkpoint("news-collection")
    if execute and not args.yes:
        raise SystemExit(
            "실제 Toss 주문은 --scenario auto-live 또는 korea-execute 와 --yes 를 함께 지정해야 실행됩니다."
        )
    risk = getattr(args, "_position_manager", None)
    if isinstance(risk, PositionRiskManager):
        await asyncio.to_thread(risk.entry_watch.cancel, args.korea_ticker.split(".")[0], "새 분석 시작")

    quick_model, deep_model, backend_url = resolve_models(args)
    collected_news = collect_korea_news_snapshot(args)
    checkpoint("analysis")
    collected_news_path = collected_news["path"] if collected_news and collected_news.get("ok") else None
    config = make_config(
        args, korea_mode=True, minimal_mode=False, collected_news_path=collected_news_path,
    )
    mode = "live" if execute else "dry-run"
    if not execute and args.date == today_korea().isoformat():
        config["account_context"] = paper_account(LOCAL_DIR, args.paper_capital).trading_context(args.korea_ticker.split(".")[0])
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
        checkpoint("planning")
        stage = "PLANNING"
        plan = plan_from_decision(
            final_state.get("final_trade_decision") or decision,
            args.korea_ticker, max_order_amount=order_budget(args),
            final_rating=final_state.get("final_rating"),
        )
        store.event(run_id, "PLANNED", {"plan": plan.__dict__, "reportPath": str(report_path)})
        if plan.action == "REVIEW":
            execution = {"ok": False, "executed": False, "dryRun": not execute,
                         "skipped": "decision-review", "plan": plan.__dict__}
        elif getattr(args, "_analysis_only", False):
            execution = {"executed": False, "dryRun": not execute, "skipped": "analysis-only", "plan": plan.__dict__}
        elif args.scenario in AUTO_SCENARIOS and (args.run_mode == "daily-market" or execute) and not is_regular_market_open(
            datetime.now(KOREA_TZ), open_text=args.market_open, close_text=args.market_close,
            holidays=holiday_dates(args.holiday_dates),
        ):
            execution = {"executed": False, "dryRun": not execute, "skipped": "market-closed", "plan": plan.__dict__}
        else:
            checkpoint("order-preparation")
            if execute:
                require_resolved_live_orders()
            stage = "ORDER_ATTEMPT"
            # Persist intent before any external side effect. The broker journal
            # remains authoritative if the process dies before RESULT is saved.
            store.event(run_id, stage, {"plan": plan.__dict__, "fillVerified": False})
            risk = getattr(args, "_position_manager", None)
            if risk:
                executor = risk.execute_with_watch if args.run_mode == "daily-market" else risk.execute
                execution = await asyncio.to_thread(executor, plan, run_id)
            else:
                execution = await execute_plan(
                    plan, execute=execute,
                    live_order_log=LOCAL_DIR / "live_orders" / "orders.jsonl",
                    experience_run_id=run_id,
                    **({"paper_account_path": LOCAL_DIR / "accounts" / "paper.sqlite3", "paper_capital": args.paper_capital} if not execute and args.date == today_korea().isoformat() else {}),
                )
        stage = "RESULT_PERSISTENCE"
        store.event(run_id, "RESULT", {"execution": execution, "fillVerified": execution.get("fillVerified", False), "realizedPnl": None})
    except BaseException as exc:
        store.event(run_id, "INTERRUPTED" if isinstance(exc, RunStopped) or not isinstance(exc, Exception) else "ERROR", {
            "stage": stage, "error": str(exc), "errorType": type(exc).__name__,
            "newsExecution": getattr(exc, "news_execution", None),
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
        "ok": execution.get("ok", (execution.get("paperExecution") or {}).get("status") != "SIMULATED_REJECTED"),
        "decision": decision,
        "reportPath": str(report_path),
        "collectedNews": collected_news,
        "newsExecution": final_state.get("news_execution"),
        "accountContext": config.get("account_context"),
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
    slot = active_trading_slot(datetime.now(KOREA_TZ))
    requested_slot = getattr(args, "_schedule_slot", None)
    if args.run_mode == "daily-market" and requested_slot is not None and requested_slot != slot:
        raise ValueError("Scheduled trading slot is no longer current.")
    args._schedule_slot = slot
    if has_scheduler_completed(run_date, "auto-live", slot):
        raise RuntimeError("A live run was already started in this time slot; automatic replay is disabled.")
    mark_live_run_started(run_date, slot)
    summary = await run_auto_trading(args, execute=True)
    mark_scheduler_completed(run_date, summary.get("summaryPath"), "completed" if summary["ok"] else "completed-with-errors", scenario="auto-live", slot=slot)
    return summary


def screen_candidate_snapshot(args, snapshot):
    settings = {key: getattr(args, key, value) for key, value in INTRADAY_DEFAULTS.items()}
    settings.update(market_open=args.market_open, market_close=args.market_close,
                    holiday_dates=args.holiday_dates, buy_cutoff_minutes=args.buy_cutoff_minutes)
    risk = getattr(args, "_position_manager", None)
    backend = risk.backend if isinstance(risk, PositionRiskManager) else BrokerBackend(LOCAL_DIR, False, args.paper_capital)
    screening = screen_candidates(snapshot["candidates"], backend, settings, requested_date=args.date)
    snapshot["candidates"] = screening.pop("rows")
    snapshot["intradayScreen"] = screening
    snapshot["warnings"] = list(dict.fromkeys([
        *(snapshot.get("warnings") or []), *(screening.get("warnings") or []),
    ]))
    snapshot["selectionReason"] = screening["error"] or (None if screening["eligibleCount"] else "단타 시장자료 조건을 통과한 후보 없음; 신규 분석/주문 생략")
    if screening["status"] == "failed":
        snapshot["ok"] = False
    if snapshot.get("jsonPath"):
        path = Path(snapshot["jsonPath"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(candidates=snapshot["candidates"], intradayScreen=screening,
                       selectionReason=snapshot["selectionReason"], ok=snapshot["ok"],
                       warnings=snapshot["warnings"])
        atomic_write_json(path, payload)
    if snapshot.get("markdownPath"):
        path = Path(snapshot["markdownPath"])
        lines = ["", "## Intraday Market Ranking", "", f"- As of: {screening['asOf']}",
                 f"- Status: {screening['status']}", "- Heuristic score, not expected profit or success probability."]
        reference = screening.get("marketReference")
        if reference:
            lines.append(f"- KOSPI / stock comparison window: {reference['basisStart']} to {reference['barEnd']}")
        lines.extend(f"- Warning: {warning}" for warning in screening.get("warnings", []))
        lines.extend(["", "| Ticker | News rank | Intraday score | Eligible | Reasons |", "|---|---:|---:|---|---|"])
        for row in snapshot["candidates"]:
            check = row["intraday"]
            reason = "; ".join(check["reasons"]).replace("|", "/").replace("\n", " ")
            lines.append(f"| {row['ticker']} | {row.get('newsRank', '-')} | {check['score']} | {check['eligible']} | {reason} |")
        path.write_text(path.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8")
    return snapshot


async def run_auto_trading(args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    checkpoint("candidate-discovery")
    if execute and not args.yes:
        raise SystemExit("Actual Toss orders require --yes.")
    scenario = "auto-live" if execute else "auto-paper"
    if not args.date:
        args.date = today_korea().isoformat()
    quick_model, deep_model, backend_url = resolve_models(args)
    candidate_snapshot = collect_candidate_snapshot(args)
    checkpoint("candidates-collected")
    selection_ok = candidate_snapshot.get("ok") is True
    selection_reason = candidate_snapshot.get("selectionReason") or candidate_snapshot.get("error")
    if not selection_ok and not selection_reason:
        selection_reason = "단타 후보 수집 실패; 신규 후보 분석/주문 생략"
    candidates = []
    if selection_ok:
        try:
            universe = validate_universe(candidate_snapshot.get("universe"))
            candidates = candidate_snapshot.get("candidates", [])
            if any(row["ticker"] not in universe["members"] for row in candidates):
                raise UniverseError("코스피200 구성목록 밖의 후보가 포함되어 신규 분석/주문을 차단했습니다.")
        except (ValueError, KeyError, TypeError) as exc:
            selection_ok, selection_reason, candidates = False, str(exc), []
    if selection_ok and candidates and args.trade_candidates > 0:
        candidate_snapshot = await asyncio.to_thread(screen_candidate_snapshot, args, candidate_snapshot)
        checkpoint("candidate-screened")
        selection_ok = candidate_snapshot["ok"]
        selection_reason = candidate_snapshot.get("selectionReason")
        candidates = [row for row in candidate_snapshot["candidates"] if row["intraday"]["eligible"]] if selection_ok else []
    candidate_names = {row["ticker"]: row.get("name") for row in candidates}
    tickers = [row["ticker"] for row in candidates[: max(args.trade_candidates, 0)]]
    if not tickers and not selection_reason:
        selection_reason = "분석 종목 수가 0으로 설정됨" if args.trade_candidates == 0 else "코스피200 뉴스 후보 없음; 신규 후보 분석/주문 생략"

    analyses: list[dict[str, Any]] = []
    original_ticker = args.korea_ticker
    try:
        for ticker in tickers:
            checkpoint("next-candidate")
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
                result["candidateScreen"] = next((row.get("intraday") for row in candidates if row["ticker"] == ticker), None)
            except RunStopped:
                raise
            except Exception as exc:
                result = {
                    "scenario": "korea-execute" if execute else "korea-dry-run",
                    "ticker": ticker,
                    "date": args.date,
                    "provider": args.provider,
                    "candidateRank": candidate_rank,
                    "ok": False,
                    "error": str(exc),
                    "newsExecution": getattr(exc, "news_execution", None),
                }
            result["name"] = result.get("name") or candidate_names.get(ticker)
            analyses.append(result)
    finally:
        args.korea_ticker = original_ticker

    summary = {
        "ok": selection_ok and all(row.get("ok", True) for row in analyses) and len(analyses) == len(tickers),
        "selectionReason": selection_reason,
        "scenario": scenario,
        "executionMode": "live" if execute else "dry-run",
        "date": args.date,
        "provider": args.provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "paperCapital": args.paper_capital,
        "positionPct": args.position_pct,
        "targetScope": "position",
        "positionTargetReturn": getattr(args, "position_target_return", ".01"),
        "perTradeBudget": str(order_budget(args)),
        "tradingObjective": trading_objective(args),
        "runMode": args.run_mode,
        "scheduleSlot": getattr(args, "_schedule_slot", None),
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
    output_path = output_dir / f"{prefix}_{args.date}_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
    atomic_write_json(output_path, summary)
    summary["summaryPath"] = str(output_path)
    return summary


def require_resolved_live_orders():
    path = LOCAL_DIR / "live_orders/orders.jsonl"
    if not path.exists():
        return
    latest = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        order_id = row.get("payload", {}).get("clientOrderId")
        if order_id:
            latest[order_id] = row.get("status")
    if any(status in {"SUBMITTING", "UNCONFIRMED"} for status in latest.values()):
        raise ValueError("접수 결과가 불확실한 실제 주문이 있습니다. Toss 주문 내역과 로그를 대조하기 전 새 주문을 차단합니다.")


async def run_manual_job(base_args, control, job):
    # Freeze settings when queued; later web edits cannot mutate a running job.
    args = argparse.Namespace(**deepcopy({key: value for key, value in vars(base_args).items() if key not in {"_controller", "_position_manager"}}))
    args._position_manager = getattr(base_args, "_position_manager", None)
    for key, value in job["settings"].items():
        setattr(args, key, value)
    args.date = today_korea().isoformat()
    args.korea_ticker = job["ticker"]
    args.skip_news_collection = False
    args._analysis_only = job["action"] == "analyze"
    args.run_mode = "daily-market"
    result = None
    status = "completed"
    reserved_slot = None
    write_monitor_status("running-manual", executionMode="live" if control.live else "dry-run",
                         scenario=args.scenario, action=job["action"], jobId=job["id"])
    try:
        with control.scope():
            checkpoint("starting")
            if job["action"] == "news":
                result = collect_korea_news_snapshot(args)
            elif job["action"] == "candidates":
                result = collect_candidate_snapshot(args)
            elif job["action"] == "analyze":
                result = await run_korea_toss(args, execute=control.live)
            else:
                if not is_regular_market_open(datetime.now(KOREA_TZ), open_text=args.market_open,
                                              close_text=args.market_close, holidays=holiday_dates(args.holiday_dates)):
                    raise ValueError("대기 중 정규장이 종료되어 매매를 실행하지 않았습니다.")
                if control.live:
                    require_resolved_live_orders()
                slot = active_trading_slot(datetime.now(KOREA_TZ))
                args._schedule_slot = slot
                if not has_scheduler_completed(args.date, args.scenario, slot):
                    mark_run_started(args.date, args.scenario, slot)
                    reserved_slot = slot
                result = await run_auto_trading(args, execute=control.live)
            checkpoint("finished")
            if isinstance(result, dict) and result.get("ok") is False:
                status = "completed-with-errors"
    except RunStopped as exc:
        status, result = "stopped", {"ok": False, "error": str(exc)}
    except Exception as exc:
        status, result = "failed", {"ok": False, "error": str(exc)}
    finally:
        control.finish(status, result)
        if reserved_slot:
            mark_scheduler_completed(args.date, (result or {}).get("summaryPath"), status,
                                     scenario=args.scenario, slot=reserved_slot)
        write_monitor_status(status, executionMode="live" if control.live else "dry-run",
                             scenario=args.scenario, action=job["action"], jobId=job["id"])


async def scheduler_wait(args, seconds):
    control = getattr(args, "_controller", None)
    if control:
        await asyncio.to_thread(control.wait, seconds)
    else:
        await asyncio.sleep(seconds)


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
        "additionalRunTime": AFTERNOON_RUN_TIME,
        "paperCapital": args.paper_capital,
        "targetScope": "position",
        "positionTargetReturn": getattr(args, "position_target_return", ".01"),
        "perTradeBudget": str(order_budget(args)),
        "holidayDates": sorted(day.isoformat() for day in holidays),
    }
    write_monitor_status("scheduler-started", **startup_status)
    print(json.dumps({"status": "scheduler-started", **startup_status}, ensure_ascii=False), flush=True)
    while True:
        control = getattr(args, "_controller", None)
        if control:
            job = control.start()
            if job:
                await run_manual_job(args, control, job)
                continue
            if control.snapshot()["paused"]:
                await scheduler_wait(args, max(args.scheduler_poll_seconds, 1))
                continue
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
            slot = active_trading_slot(now)
            entry_ready = datetime.combine(now.date(), parse_hhmm(args.market_open), tzinfo=KOREA_TZ) + timedelta(minutes=20)
            if slot == "morning" and now < entry_ready:
                write_monitor_status("waiting-for-completed-bars", **mode_status,
                                     nextRunAfter=entry_ready.isoformat(timespec="seconds"))
                await scheduler_wait(args, min(max(1, int((entry_ready - now).total_seconds())), max(args.scheduler_poll_seconds, 1)))
                continue
            if has_scheduler_completed(run_date, scenario, slot):
                next_open = next_trading_slot_time(now, args, holidays)
                wait_seconds = max(1, int((next_open - now).total_seconds()))
                status_payload = {
                    **mode_status,
                    "scheduler": "daily-market",
                    "date": run_date,
                    "scheduleSlot": slot,
                    "nextRunAfter": next_open.isoformat(timespec="seconds"),
                }
                outcome = load_scheduler_state(scenario)["runs"][run_date]["slots"][slot]["status"]
                if outcome == "started":
                    outcome = "interrupted-live-run" if scenario == "auto-live" else "interrupted-paper-run"
                wait_status = "waiting-for-afternoon-run" if next_open.date() == now.date() else "already-completed"
                if outcome != "completed":
                    wait_status = outcome
                write_monitor_status(wait_status, **status_payload)
                print(
                    json.dumps({"status": wait_status, **status_payload}, ensure_ascii=False),
                    flush=True,
                )
                await scheduler_wait(args, min(wait_seconds, max(args.scheduler_poll_seconds, 1)))
                continue

            if control:
                job = control.start(scheduled_job())
                if not job:
                    continue
                if job["source"] == "manual":
                    await run_manual_job(args, control, job)
                    continue
            args.date = run_date
            args._schedule_slot = slot
            status_payload = {**mode_status, "scheduler": "daily-market", "date": run_date, "scheduleSlot": slot}
            write_monitor_status(f"running-{scenario}", **status_payload)
            print(json.dumps({"status": f"running-{scenario}", **status_payload}, ensure_ascii=False), flush=True)
            try:
                with control.scope() if control else nullcontext():
                    if scenario == "auto-paper":
                        mark_run_started(run_date, scenario, slot)
                    summary = await (run_auto_live(args) if scenario == "auto-live" else run_auto_paper(args))
            except Exception as exc:
                summary = {**mode_status, "ok": False, "date": run_date, "scheduleSlot": slot, "error": str(exc)}
                if isinstance(exc, RunStopped):
                    summary["stopped"] = True
                prefix = scenario.replace("-", "_")
                error_path = LOCAL_DIR / prefix / f"{prefix}_{run_date}_{slot}_failed.json"
                atomic_write_json(error_path, summary)
                summary["summaryPath"] = str(error_path)
            # Record partial failure separately and avoid replaying successful
            # orders automatically within a slot. The next slot is independent.
            outcome = "stopped" if summary.get("stopped") else "completed" if summary.get("ok", True) else "completed-with-errors"
            if control:
                control.finish(outcome, summary)
            mark_scheduler_completed(run_date, summary.get("summaryPath"), outcome, scenario=scenario, slot=slot)
            write_monitor_status(
                outcome,
                **mode_status,
                scheduler="daily-market",
                date=run_date,
                scheduleSlot=slot,
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
        await scheduler_wait(args, min(wait_seconds, max(args.scheduler_poll_seconds, 1)))


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
    risk = None
    if args.scenario in AUTO_SCENARIOS | {"korea-dry-run", "korea-execute"} and (not args.date or args.date == today_korea().isoformat()):
        live = args.scenario in {"auto-live", "korea-execute"}
        if live and not args.yes:
            raise SystemExit("Live position management requires --yes.")
        risk = PositionRiskManager(LOCAL_DIR, live=live, settings=lambda: {**default_settings(args), **load_trading_settings()})
        args._position_manager = risk
    monitor_server = None
    if args.monitor:
        control = None
        if args.scenario in AUTO_SCENARIOS and args.run_mode == "daily-market":
            control = RunController(live=args.scenario == "auto-live", audit_path=LOCAL_DIR / "monitor/manual_runs.jsonl")
            args._controller = control
            if risk:
                def watch_guard():
                    state = control.snapshot()
                    if state["paused"] or state["stopRequested"]:
                        return "cancel"
                    return "wait" if state["active"] or state["pending"] else "ready"
                risk.entry_watch.guard = watch_guard
        monitor_server = start_monitor_server(args.monitor_host, args.monitor_port, scenario=args.scenario, control=control, risk=risk)
        monitor_url = getattr(monitor_server, "monitor_url", f"http://{args.monitor_host}:{args.monitor_port}")
        monitor_token = ensure_monitor_token()
        print(
            json.dumps(
                {
                    "monitor": "started",
                    "url": monitor_url,
                    "settingsTokenPath": str(monitor_token_path()),
                    "settingsTokenPreview": monitor_token[:6] + "...",
                    "loginSetup": str(PROJECT_DIR / "Setup-Web-Login.command"),
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
        if risk:
            risk.start()
        return await run_scenarios(args)
    finally:
        if risk:
            await asyncio.to_thread(risk.stop)
        if monitor_server:
            monitor_server.research.close()
            monitor_server.account_stop.set()
            monitor_server.shutdown()
            monitor_server.server_close()


async def run_scenarios(args: argparse.Namespace) -> int:
    results: list[dict[str, Any]] = []
    # Default hybrid setup (local news, OpenAI for the other roles):
    #   Put OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, and Toss keys
    #   in /Users/leejang2/Project/.env, then run:
    #   python main.py
    #   python main.py --provider openai --model gpt-5.5
    #
    # Web settings apply between runs. OpenAI-only remains selectable.
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
