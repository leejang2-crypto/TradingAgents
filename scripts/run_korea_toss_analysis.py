from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.dataflows.naver_news import collect_news_naver
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


PROJECT_DIR = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_DIR / ".local"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Korean-stock analysis and prepare/execute a Toss order.")
    parser.add_argument("--ticker", default="005930.KS", help="TradingAgents/Yahoo ticker, e.g. 005930.KS")
    parser.add_argument("--date", required=True, help="Analysis date YYYY-MM-DD")
    parser.add_argument(
        "--provider",
        choices=["openai", "ollama", "hybrid"],
        default="openai",
        help=(
            "LLM provider. Use hybrid to draft with Ollama and decide with OpenAI."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model for both quick/deep agents. Examples: openai -> gpt-5.5, "
            "ollama -> qwen3:8b or gemma3:12b."
        ),
    )
    parser.add_argument("--quick-model", help="Override the model used by faster analyst/debate agents.")
    parser.add_argument("--deep-model", help="Override the model used by manager/portfolio agents.")
    parser.add_argument(
        "--backend-url",
        help="Optional backend URL. For Ollama default is http://localhost:11434/v1.",
    )
    parser.add_argument("--language", default="Korean")
    parser.add_argument("--max-order-amount", default="100000", help="KRW budget for generated BUY plan")
    parser.add_argument("--news-display", type=int, default=20, help="Naver news collection count.")
    parser.add_argument(
        "--skip-news-collection",
        action="store_true",
        help="Do not pre-collect Naver stock news before analysis.",
    )
    parser.add_argument("--execute", action="store_true", help="Send the generated Toss order")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation when --execute is set")
    return parser


def collect_news_snapshot(args: argparse.Namespace) -> dict[str, object] | None:
    if args.skip_news_collection:
        return None
    end_date = datetime.strptime(args.date, "%Y-%m-%d")
    start_date = (end_date - timedelta(days=7)).strftime("%Y-%m-%d")
    output_path = LOCAL_DIR / "news" / f"analysis_naver_{args.ticker.replace('.', '_')}_{args.date}.json"
    try:
        articles = collect_news_naver(
            args.ticker,
            start_date,
            args.date,
            display=args.news_display,
            sort="date",
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": None, "articleCount": 0}

    payload = {
        "source": "Naver Search API",
        "purpose": "TradingAgents pre-analysis news snapshot",
        "startDate": start_date,
        "endDate": args.date,
        "tickers": [args.ticker],
        "articleCount": len(articles),
        "articles": articles,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(output_path), "articleCount": len(articles)}


async def main_async() -> int:
    args = make_parser().parse_args()
    memory_path = LOCAL_DIR / "memory" / "trading_memory.md"
    if args.provider == "hybrid":
        quick_model = args.quick_model or args.model or "qwen3:8b"
        deep_model = args.deep_model or args.model or "gpt-5.5"
        backend_url = args.backend_url or "http://localhost:11434/v1"
    else:
        default_model = "qwen3:8b" if args.provider == "ollama" else "gpt-5.5"
        quick_model = args.quick_model or args.model or default_model
        deep_model = args.deep_model or args.model or default_model
        backend_url = args.backend_url
    if args.provider == "ollama" and not backend_url:
        backend_url = "http://localhost:11434/v1"

    # Local Ollama guide:
    #   1. Install: brew install --cask ollama
    #   2. Pull model: ollama pull qwen3:8b
    #   3. Run this script:
    #      ../.venv/bin/python scripts/run_korea_toss_analysis.py \
    #        --provider ollama --model qwen3:8b --ticker 005930.KS --date 2026-07-08
    #
    # OpenAI guide:
    #   1. Put OPENAI_API_KEY in /Users/leejang2/Project/.env
    #   2. Run with --provider openai --model gpt-5.5
    #
    # Hybrid guide:
    #   Ollama drafts lower-risk agents; OpenAI makes research/trade/portfolio decisions.
    #   ../.venv/bin/python scripts/run_korea_toss_analysis.py \
    #      --provider hybrid --quick-model qwen3:8b --deep-model gpt-5.5 \
    #      --ticker 005930.KS --date 2026-07-10

    collected_news = collect_news_snapshot(args)
    collected_news_path = collected_news["path"] if collected_news and collected_news.get("ok") else None

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

    graph = TradingAgentsGraph(selected_analysts=("market", "news"), debug=False, config=config)
    final_state, decision = graph.propagate(args.ticker, args.date)
    report_path = graph.save_reports(final_state, args.ticker)

    plan = plan_from_decision(
        final_state.get("final_trade_decision") or decision,
        args.ticker,
        max_order_amount=Decimal(args.max_order_amount),
    )
    if args.execute and not args.yes:
        print(json.dumps({"plan": plan.__dict__}, ensure_ascii=False, indent=2))
        answer = input("실제 Toss 주문을 전송하려면 YES를 입력하세요: ").strip()
        if answer != "YES":
            print("주문 실행을 취소했습니다. 분석 리포트는 저장되었습니다.")
            print(f"report_path: {report_path}")
            return 1

    execution = await execute_plan(plan, execute=args.execute)
    print(json.dumps(
        {
            "ticker": args.ticker,
            "date": args.date,
            "provider": args.provider,
            "quickModel": quick_model,
            "deepModel": deep_model,
            "backendUrl": backend_url,
            "decision": decision,
            "reportPath": str(report_path),
            "collectedNews": collected_news,
            "toss": execution,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
