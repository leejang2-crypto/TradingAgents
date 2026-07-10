from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.dataflows.naver_news import collect_news_naver
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = PROJECT_DIR / ".local"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "TradingAgents integrated runner. By default it runs the safe scenarios: "
            "focused tests, minimal analysis, and Korean-stock Toss dry-run."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["all-safe", "tests", "minimal", "korea-dry-run", "korea-execute"],
        default="all-safe",
        help="Execution scenario. all-safe never sends a real Toss order.",
    )
    parser.add_argument("--ticker", default="AAPL", help="Ticker for the minimal scenario.")
    parser.add_argument("--korea-ticker", default="005930.KS", help="Korean ticker, e.g. 005930.KS.")
    parser.add_argument("--date", default="2024-05-10", help="Analysis date YYYY-MM-DD.")
    parser.add_argument(
        "--provider",
        choices=["openai", "ollama", "hybrid"],
        default="openai",
        help=(
            "LLM provider. hybrid routes draft agents to Ollama and "
            "decision-critical agents to OpenAI."
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
    parser.add_argument("--max-order-amount", default="100000", help="KRW budget for BUY dry-run.")
    parser.add_argument("--news-display", type=int, default=20, help="Naver news collection count.")
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
    if args.provider == "hybrid":
        quick_model = args.quick_model or args.model or "qwen3:8b"
        deep_model = args.deep_model or args.model or "gpt-5.5"
        backend_url = args.backend_url or "http://localhost:11434/v1"
        return quick_model, deep_model, backend_url

    default_model = "qwen3:8b" if args.provider == "ollama" else "gpt-5.5"
    quick_model = args.quick_model or args.model or default_model
    deep_model = args.deep_model or args.model or default_model
    backend_url = args.backend_url
    if args.provider == "ollama" and not backend_url:
        backend_url = "http://localhost:11434/v1"
    return quick_model, deep_model, backend_url


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
            "backend_url": None if args.provider == "hybrid" else backend_url,
            "ollama_backend_url": (
                backend_url if args.provider == "hybrid"
                else DEFAULT_CONFIG.get("ollama_backend_url")
            ),
            "output_language": args.language,
            "results_dir": str(LOCAL_DIR / "logs"),
            "data_cache_dir": str(LOCAL_DIR / "cache"),
            "memory_log_path": str(memory_path),
            "checkpoint_enabled": False,
        }
    )
    if args.provider == "hybrid":
        config.update(
            {
                "hybrid_ollama_quick_llm": quick_model,
                "hybrid_ollama_deep_llm": quick_model,
                "hybrid_openai_quick_llm": deep_model,
                "hybrid_openai_deep_llm": deep_model,
                "local_safe_validation": True,
            }
        )
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


def run_focused_tests() -> dict[str, Any]:
    tests = [
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
        max_order_amount=Decimal(args.max_order_amount),
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


async def main_async() -> int:
    args = make_parser().parse_args()
    results: list[dict[str, Any]] = []

    # Recommended local Ollama setup:
    #   launchctl setenv OLLAMA_MODELS /Volumes/External/OllamaModels
    #   open -a Ollama
    #   ollama pull qwen3:8b
    #   ollama pull gemma3:12b
    #   python main.py --provider ollama --quick-model qwen3:8b --deep-model gemma3:12b
    #
    # Recommended OpenAI setup:
    #   Put OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, and Toss keys
    #   in /Users/leejang2/Project/.env, then run:
    #   python main.py --provider openai --model gpt-5.5
    #
    # Recommended hybrid setup:
    #   Ollama drafts market/news/bull/bear/aggressive/neutral agents while
    #   OpenAI handles research manager/trader/conservative/portfolio decisions.
    #   Local-safe validation forces HOLD when local output is mechanically unsafe.
    #   python main.py --provider hybrid --quick-model qwen3:8b --deep-model gpt-5.5

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
