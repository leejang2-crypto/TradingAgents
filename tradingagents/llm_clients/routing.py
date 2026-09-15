"""Shared role names and conservative local-news routing defaults."""

AGENT_LABELS = {
    "market_analyst": "시장 분석",
    "sentiment_analyst": "심리 분석",
    "news_analyst": "뉴스 분석",
    "fundamentals_analyst": "재무 분석",
    "bull_researcher": "강세 리서치",
    "bear_researcher": "약세 리서치",
    "research_manager": "리서치 매니저",
    "trader": "트레이더",
    "risk_aggressive": "공격적 위험 분석",
    "risk_neutral": "중립 위험 분석",
    "risk_conservative": "보수적 위험 분석",
    "portfolio_manager": "포트폴리오 매니저",
}
DEEP_AGENTS = {"research_manager", "portfolio_manager"}
NEWS_ONLY_ROUTING = {
    name: "ollama" if name == "news_analyst" else "openai" for name in AGENT_LABELS
}
LLM_DEFAULTS = {
    "provider": "hybrid",
    "openai_quick_model": "gpt-5.5",
    "openai_deep_model": "gpt-5.5",
    "ollama_quick_model": "qwen3:8b",
    "ollama_deep_model": "gemma3:12b",
    "ollama_base_url": "http://localhost:11434/v1",
    "hybrid_agent_providers": NEWS_ONLY_ROUTING,
}


def describe_routing(config: dict) -> dict[str, dict[str, str]]:
    provider = config["llm_provider"]
    routes = {}
    for agent in AGENT_LABELS:
        kind = "deep" if agent in DEEP_AGENTS else "quick"
        selected = config.get("hybrid_agent_providers", {}).get(agent, "openai") if provider == "hybrid" else provider
        model = config[f"hybrid_{selected}_{kind}_llm"] if provider == "hybrid" else config[f"{kind}_think_llm"]
        routes[agent] = {"provider": selected, "model": model}
    return routes
