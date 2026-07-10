from tradingagents.agents.utils.local_safe import validate_trading_state
from tradingagents.graph.trading_graph import TradingAgentsGraph


def test_hybrid_graph_builds_agent_llm_routing(monkeypatch, tmp_path):
    created = []

    class DummyClient:
        def __init__(self, provider, model, base_url):
            self.provider = provider
            self.model = model
            self.base_url = base_url

        def get_llm(self):
            return f"{self.provider}:{self.model}:{self.base_url}"

    def fake_create_llm_client(provider, model, base_url=None, **kwargs):
        created.append((provider, model, base_url, kwargs))
        return DummyClient(provider, model, base_url)

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        fake_create_llm_client,
    )

    config = {
        "llm_provider": "hybrid",
        "deep_think_llm": "gpt-5.5",
        "quick_think_llm": "qwen3:8b",
        "hybrid_openai_quick_llm": "gpt-5.5",
        "hybrid_openai_deep_llm": "gpt-5.5",
        "hybrid_ollama_quick_llm": "qwen3:8b",
        "hybrid_ollama_deep_llm": "qwen3:8b",
        "ollama_backend_url": "http://localhost:11434/v1",
        "backend_url": None,
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "logs"),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 50,
        "checkpoint_enabled": False,
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
            "macro_data": "fred",
            "prediction_markets": "polymarket",
        },
        "tool_vendors": {},
        "hybrid_agent_providers": {
            "market_analyst": "ollama",
            "news_analyst": "ollama",
            "bull_researcher": "ollama",
            "bear_researcher": "ollama",
            "research_manager": "openai",
            "trader": "openai",
            "risk_aggressive": "ollama",
            "risk_neutral": "ollama",
            "risk_conservative": "openai",
            "portfolio_manager": "openai",
        },
    }

    graph = TradingAgentsGraph(selected_analysts=("market",), config=config)

    assert ("openai", "gpt-5.5", None, {}) in created
    assert ("ollama", "qwen3:8b", "http://localhost:11434/v1", {}) in created
    assert graph.agent_llms["market_analyst"].startswith("ollama:qwen3:8b")
    assert graph.agent_llms["research_manager"].startswith("openai:gpt-5.5")
    assert graph.agent_llms["portfolio_manager"].startswith("openai:gpt-5.5")


def test_local_safe_validation_catches_observed_ollama_failures():
    final_state = {
        "market_report": "종가: 285,000원\nTool call get_stock_data(ticker=\"005060\")",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "investment_plan": "**Rating**: Underweight",
        "trader_investment_plan": "**Action**: Sell\n\n**Stop Loss**: 850000.0",
        "final_trade_decision": "## 최종 거래 결정: **매",
    }

    result = validate_trading_state(final_state, "005930.KS")

    assert not result.ok
    assert any("005060" in reason for reason in result.reasons)
    assert any("stop loss" in reason for reason in result.reasons)
    assert any("truncated" in reason for reason in result.reasons)
