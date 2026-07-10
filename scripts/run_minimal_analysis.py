from __future__ import annotations

import argparse
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


PROJECT_DIR = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_DIR / ".local"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a minimal TradingAgents analysis.")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--date", default="2024-05-10")
    parser.add_argument(
        "--provider",
        choices=["openai", "ollama", "hybrid"],
        default="openai",
        help="Use openai, ollama, or hybrid local drafts plus OpenAI decisions.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model for both quick/deep agents. Examples: gpt-5.5, qwen3:8b, gemma3:12b.",
    )
    parser.add_argument("--quick-model", help="Override quick agent model.")
    parser.add_argument("--deep-model", help="Override deep manager model.")
    parser.add_argument("--backend-url", help="Optional backend URL. Ollama default: http://localhost:11434/v1.")
    parser.add_argument("--language", default="Korean")
    return parser


def main() -> int:
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

    # Ollama local guide:
    #   brew install --cask ollama
    #   ollama pull qwen3:8b
    #   ../.venv/bin/python scripts/run_minimal_analysis.py --provider ollama --model qwen3:8b
    #
    # OpenAI guide:
    #   Put OPENAI_API_KEY in /Users/leejang2/Project/.env, then use
    #   --provider openai --model gpt-5.5
    #
    # Hybrid guide:
    #   ../.venv/bin/python scripts/run_minimal_analysis.py \
    #     --provider hybrid --quick-model qwen3:8b --deep-model gpt-5.5

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
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "checkpoint_enabled": False,
            "results_dir": str(LOCAL_DIR / "logs"),
            "data_cache_dir": str(LOCAL_DIR / "cache"),
            "memory_log_path": str(memory_path),
            "news_article_limit": 3,
            "global_news_article_limit": 3,
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

    graph = TradingAgentsGraph(
        selected_analysts=("market",),
        debug=False,
        config=config,
    )
    final_state, decision = graph.propagate(args.ticker, args.date)
    report_path = graph.save_reports(final_state, args.ticker)

    print("TradingAgents minimal analysis complete")
    print(f"ticker: {args.ticker}")
    print(f"date: {args.date}")
    print(f"provider: {args.provider}")
    print(f"quick_model: {quick_model}")
    print(f"deep_model: {deep_model}")
    print(f"backend_url: {backend_url}")
    print(f"decision: {decision}")
    print(f"report_path: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
