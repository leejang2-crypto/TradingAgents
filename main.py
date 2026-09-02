from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
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
    print(
        json.dumps(
            {
                "scheduler": "daily-market",
                "status": "started",
                "timezone": "Asia/Seoul",
                "marketOpen": args.market_open,
                "marketClose": args.market_close,
                "paperCapital": args.paper_capital,
                "dailyTargetReturn": args.daily_target_return,
                "dailyTargetProfit": str(daily_profit_target(args)),
                "perTradeBudget": str(order_budget(args)),
                "holidayDates": sorted(day.isoformat() for day in holidays),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
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
                print(
                    json.dumps(
                        {
                            "scheduler": "daily-market",
                            "status": "already-completed",
                            "date": run_date,
                            "nextRunAfter": next_open.isoformat(timespec="seconds"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))
                continue

            args.date = run_date
            print(
                json.dumps(
                    {
                        "scheduler": "daily-market",
                        "status": "running-auto-paper",
                        "date": run_date,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            summary = await run_auto_paper(args)
            mark_scheduler_completed(run_date, summary.get("summaryPath"))
            print(json.dumps({"results": [summary]}, ensure_ascii=False, indent=2), flush=True)
            continue

        next_open = next_regular_market_open(
            now,
            open_text=args.market_open,
            close_text=args.market_close,
            holidays=holidays,
        )
        wait_seconds = max(1, int((next_open - now).total_seconds()))
        print(
            json.dumps(
                {
                    "scheduler": "daily-market",
                    "status": "waiting-for-market-open",
                    "now": now.isoformat(timespec="seconds"),
                    "nextRunAfter": next_open.isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        await asyncio.sleep(min(wait_seconds, max(args.scheduler_poll_seconds, 1)))


async def main_async() -> int:
    args = make_parser().parse_args()
    results: list[dict[str, Any]] = []

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
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
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
