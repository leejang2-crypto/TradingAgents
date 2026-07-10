from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingagents.brokers.toss_executor import plan_from_decision
from tradingagents.dataflows.naver_news import collect_news_naver
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


PROJECT_DIR = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_DIR / ".local"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare TradingAgents outputs from OpenAI and local Ollama models."
    )
    parser.add_argument("--ticker", default="005930.KS", help="Ticker to analyze, e.g. 005930.KS.")
    parser.add_argument("--date", required=True, help="Analysis date YYYY-MM-DD.")
    parser.add_argument("--language", default="Korean")
    parser.add_argument(
        "--analysts",
        default="market,news",
        help="Comma-separated analyst set. Recommended for Korea: market,news.",
    )
    parser.add_argument("--openai-model", default="gpt-5.5")
    parser.add_argument("--ollama-quick-model", default="qwen3:8b")
    parser.add_argument("--ollama-deep-model", default="gemma3:12b")
    parser.add_argument("--ollama-url", default="http://localhost:11434/v1")
    parser.add_argument("--max-order-amount", default="100000")
    parser.add_argument("--news-display", type=int, default=20)
    parser.add_argument(
        "--skip-news-collection",
        action="store_true",
        help="Use live configured news vendors instead of a shared Naver snapshot.",
    )
    return parser


def _parse_analysts(value: str) -> tuple[str, ...]:
    analysts = tuple(item.strip() for item in value.split(",") if item.strip())
    return analysts or ("market", "news")


def _safe_name(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace(".", "_")


def collect_news_snapshot(args: argparse.Namespace, output_dir: Path) -> dict[str, Any] | None:
    if args.skip_news_collection:
        return None
    end_date = datetime.strptime(args.date, "%Y-%m-%d")
    start_date = (end_date - timedelta(days=7)).strftime("%Y-%m-%d")
    output_path = output_dir / f"shared_naver_news_{_safe_name(args.ticker)}_{args.date}.json"
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
        "purpose": "OpenAI vs Ollama comparison shared news snapshot",
        "startDate": start_date,
        "endDate": args.date,
        "tickers": [args.ticker],
        "articleCount": len(articles),
        "articles": articles,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(output_path), "articleCount": len(articles)}


def make_config(
    *,
    provider: str,
    quick_model: str,
    deep_model: str,
    backend_url: str | None,
    language: str,
    output_dir: Path,
    collected_news_path: str | None,
) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": provider,
            "quick_think_llm": quick_model,
            "deep_think_llm": deep_model,
            "backend_url": backend_url,
            "output_language": language,
            "results_dir": str(output_dir / "logs"),
            "data_cache_dir": str(output_dir / "cache"),
            "memory_log_path": str(output_dir / "memory" / "trading_memory.md"),
            "checkpoint_enabled": False,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
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


def run_provider(
    *,
    label: str,
    provider: str,
    quick_model: str,
    deep_model: str,
    backend_url: str | None,
    args: argparse.Namespace,
    output_dir: Path,
    collected_news_path: str | None,
) -> dict[str, Any]:
    started = time.monotonic()
    provider_dir = output_dir / label
    config = make_config(
        provider=provider,
        quick_model=quick_model,
        deep_model=deep_model,
        backend_url=backend_url,
        language=args.language,
        output_dir=provider_dir,
        collected_news_path=collected_news_path,
    )
    graph = TradingAgentsGraph(
        selected_analysts=_parse_analysts(args.analysts),
        debug=False,
        config=config,
    )
    final_state, decision = graph.propagate(args.ticker, args.date)
    report_path = graph.save_reports(final_state, args.ticker, provider_dir / "report")
    plan = plan_from_decision(
        final_state.get("final_trade_decision") or decision,
        args.ticker,
        max_order_amount=Decimal(args.max_order_amount),
    )
    elapsed = round(time.monotonic() - started, 2)
    state_path = provider_dir / "state_summary.json"
    summary = {
        "label": label,
        "provider": provider,
        "quickModel": quick_model,
        "deepModel": deep_model,
        "backendUrl": backend_url,
        "decision": decision,
        "elapsedSeconds": elapsed,
        "reportPath": str(report_path),
        "plan": plan.__dict__,
        "sections": extract_sections(final_state),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["stateSummaryPath"] = str(state_path)
    return summary


def extract_sections(final_state: dict[str, Any]) -> dict[str, str]:
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    return {
        "market_analyst": final_state.get("market_report", ""),
        "sentiment_analyst": final_state.get("sentiment_report", ""),
        "news_analyst": final_state.get("news_report", ""),
        "fundamentals_analyst": final_state.get("fundamentals_report", ""),
        "bull_researcher": debate.get("bull_history", ""),
        "bear_researcher": debate.get("bear_history", ""),
        "research_manager": final_state.get("investment_plan", "") or debate.get("judge_decision", ""),
        "trader": final_state.get("trader_investment_plan", ""),
        "risk_aggressive": risk.get("aggressive_history", ""),
        "risk_conservative": risk.get("conservative_history", ""),
        "risk_neutral": risk.get("neutral_history", ""),
        "portfolio_manager": final_state.get("final_trade_decision", "") or risk.get("judge_decision", ""),
    }


def _first_line(text: str, limit: int = 180) -> str:
    compact = " ".join(text.strip().split())
    if not compact:
        return "(not generated)"
    return compact[:limit] + ("..." if len(compact) > limit else "")


def write_comparison(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    collected_news: dict[str, Any] | None,
    openai_result: dict[str, Any],
    ollama_result: dict[str, Any],
) -> Path:
    openai_sections = openai_result["sections"]
    ollama_sections = ollama_result["sections"]
    all_sections = list(dict.fromkeys([*openai_sections.keys(), *ollama_sections.keys()]))

    lines = [
        f"# TradingAgents Provider Comparison: {args.ticker}",
        "",
        f"- Date: {args.date}",
        f"- Analysts: {args.analysts}",
        f"- Shared Naver news: {collected_news if collected_news else 'disabled'}",
        "",
        "## Summary",
        "",
        "| Provider | Quick model | Deep model | Decision | Plan action | Elapsed | Report |",
        "|---|---|---|---|---|---:|---|",
    ]
    for result in (openai_result, ollama_result):
        lines.append(
            "| {label} | {quick} | {deep} | {decision} | {action} | {elapsed}s | [{label} report]({report}) |".format(
                label=result["label"],
                quick=result["quickModel"],
                deep=result["deepModel"],
                decision=result["decision"],
                action=result["plan"]["action"],
                elapsed=result["elapsedSeconds"],
                report=result["reportPath"],
            )
        )

    lines.extend(
        [
            "",
            "## Agent-By-Agent Snapshot",
            "",
            "| Agent | OpenAI snapshot | Ollama snapshot |",
            "|---|---|---|",
        ]
    )
    for section in all_sections:
        lines.append(
            f"| `{section}` | {_first_line(openai_sections.get(section, ''))} | {_first_line(ollama_sections.get(section, ''))} |"
        )

    lines.extend(["", "## Full Agent Outputs"])
    for section in all_sections:
        lines.extend(
            [
                "",
                f"### {section}",
                "",
                "#### OpenAI",
                "",
                openai_sections.get(section, "") or "(not generated)",
                "",
                "#### Ollama",
                "",
                ollama_sections.get(section, "") or "(not generated)",
            ]
        )

    path = output_dir / "comparison.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    args = make_parser().parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = LOCAL_DIR / "comparisons" / f"{_safe_name(args.ticker)}_{args.date}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    collected_news = collect_news_snapshot(args, output_dir)
    collected_news_path = collected_news["path"] if collected_news and collected_news.get("ok") else None

    openai_result = run_provider(
        label="openai",
        provider="openai",
        quick_model=args.openai_model,
        deep_model=args.openai_model,
        backend_url=None,
        args=args,
        output_dir=output_dir,
        collected_news_path=collected_news_path,
    )
    ollama_result = run_provider(
        label="ollama",
        provider="ollama",
        quick_model=args.ollama_quick_model,
        deep_model=args.ollama_deep_model,
        backend_url=args.ollama_url,
        args=args,
        output_dir=output_dir,
        collected_news_path=collected_news_path,
    )

    comparison_path = write_comparison(
        args=args,
        output_dir=output_dir,
        collected_news=collected_news,
        openai_result=openai_result,
        ollama_result=ollama_result,
    )
    index = {
        "ticker": args.ticker,
        "date": args.date,
        "comparisonPath": str(comparison_path),
        "outputDir": str(output_dir),
        "collectedNews": collected_news,
        "openai": {key: value for key, value in openai_result.items() if key != "sections"},
        "ollama": {key: value for key, value in ollama_result.items() if key != "sections"},
    }
    (output_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
