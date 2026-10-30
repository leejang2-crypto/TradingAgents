import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import httpx
from openai import RateLimitError
import pytest

import main
from tradingagents.agents.utils.experience import ExperienceStore
from tradingagents.llm_clients import credit_fallback as fallback


def exhausted(code="credit_balance_exhausted", host="api.openai.com"):
    return RateLimitError("No credits", response=httpx.Response(
        429, request=httpx.Request("POST", f"https://{host}/v1/responses")),
        body={"error": {"code": code, "type": "insufficient_quota"}})


@pytest.fixture(autouse=True)
def isolate_latch():
    fallback._EXHAUSTED.clear()
    yield
    fallback._EXHAUSTED.clear()


@pytest.mark.parametrize("code,host,expected", [
    ("credit_balance_exhausted", "api.openai.com", True),
    ("insufficient_quota", "api.openai.com", False),
    ("rate_limit_exceeded", "api.openai.com", False),
    ("organization_spend_limit_exceeded", "api.openai.com", False),
    ("credit_balance_exhausted", "localhost", False),
])
def test_detection(code, host, expected):
    assert fallback.is_credit_exhausted(exhausted(code, host)) is expected


def test_wrapped_error_and_cycle():
    outer = ValueError("news failed")
    outer.__cause__ = exhausted()
    outer.__cause__.__context__ = outer
    assert fallback.is_credit_exhausted(outer)
    fake = ValueError("credit_balance_exhausted: You have no credits remaining")
    fake.__context__ = fake
    assert not fallback.is_credit_exhausted(fake)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    args = main.make_parser().parse_args([
        "--scenario", "korea-dry-run", "--date", "2026-09-01", "--yes",
    ])
    graphs, configs = [], []
    failures = [exhausted()]

    def factory(**kwargs):
        config = deepcopy(kwargs["config"])
        configs.append(config)
        graph = Mock()
        if failures:
            graph.propagate.side_effect = failures.pop(0)
        else:
            graph.propagate.return_value = ({"final_trade_decision": "**Rating**: Hold"}, "Hold")
        graph.save_reports.return_value = "report.md"
        graphs.append(graph)
        return graph

    executor = AsyncMock(return_value={"executed": False, "dryRun": True})
    monkeypatch.setattr(main, "TradingAgentsGraph", factory)
    monkeypatch.setattr(main, "execute_plan", executor)
    return args, configs, failures, executor, tmp_path


@pytest.mark.parametrize("provider", ["openai", "hybrid"])
@pytest.mark.parametrize("live", [False, True])
def test_retry_and_latch_preserve_objective_and_actual_routes(runner, provider, live):
    args, configs, failures, executor, path = runner
    args.provider = provider
    result = asyncio.run(main.run_korea_toss(args, execute=live))
    assert len(configs) == 2
    assert configs[0]["llm_provider"] == provider
    assert configs[1]["llm_provider"] == "ollama"
    assert configs[1]["quick_think_llm"] == "qwen3:8b"
    assert configs[1]["deep_think_llm"] == "gemma3:12b"
    assert configs[1]["backend_url"] == args.ollama_base_url
    assert configs[1]["trading_objective"] == configs[0]["trading_objective"]
    assert configs[1]["local_safe_validation"]
    assert configs[1]["news_local_single_pass"]
    assert not configs[1]["news_openai_fallback"]
    assert result["provider"] == "ollama" and result["requestedProvider"] == provider
    assert result["llmExecution"]["fallback"]["mode"] == "restart"
    assert [row["status"] for row in result["llmExecution"]["attempts"]] == ["failed", "completed"]
    executor.assert_awaited_once()
    store = ExperienceStore(path / "learning" / "experiences.sqlite3")
    saved = store.get(result["learning"]["runId"])
    assert all(route["provider"] == "ollama" for route in saved["routing"].values())
    assert any(event["kind"] == "LLM_FALLBACK" for event in saved["events"])
    result2 = asyncio.run(main.run_korea_toss(args, execute=live))
    assert len(configs) == 3 and configs[-1]["llm_provider"] == "ollama"
    assert result2["llmExecution"]["fallback"]["mode"] == "latched"


@pytest.mark.parametrize("error", [TimeoutError("slow"), exhausted("rate_limit_exceeded"), ValueError("credit_balance_exhausted")])
def test_unrelated_failures_never_retry(runner, error):
    args, configs, failures, executor, _ = runner
    failures[:] = [error]
    with pytest.raises(type(error)):
        asyncio.run(main.run_korea_toss(args, execute=False))
    assert len(configs) == 1 and not fallback._EXHAUSTED
    executor.assert_not_awaited()


def test_local_failure_has_audit_and_no_order(runner):
    args, configs, failures, executor, path = runner
    failures.append(ConnectionError("Ollama unavailable"))
    with pytest.raises(ConnectionError) as caught:
        asyncio.run(main.run_korea_toss(args, execute=False))
    assert len(configs) == 2
    assert caught.value.llm_execution["provider"] == "ollama"
    assert caught.value.llm_execution["attempts"][-1]["status"] == "failed"
    executor.assert_not_awaited()
    store = ExperienceStore(path / "learning" / "experiences.sqlite3")
    saved = store.get(store.recent("dry-run")[0]["id"])
    assert saved["events"][-1]["payload"]["llmExecution"]["provider"] == "ollama"


@pytest.mark.parametrize("provider,enabled", [("openai", False), ("ollama", True)])
def test_disabled_or_local_never_retry(runner, provider, enabled):
    args, configs, failures, executor, _ = runner
    args.provider, args.openai_credit_fallback = provider, enabled
    with pytest.raises(RateLimitError):
        asyncio.run(main.run_korea_toss(args, execute=False))
    assert len(configs) == 1 and not fallback._EXHAUSTED
    executor.assert_not_awaited()


def test_order_failure_never_replays_analysis_or_order(runner):
    args, configs, failures, executor, _ = runner
    failures.clear()
    executor.side_effect = exhausted()
    with pytest.raises(RateLimitError):
        asyncio.run(main.run_korea_toss(args, execute=False))
    assert len(configs) == 1 and not fallback._EXHAUSTED
    executor.assert_awaited_once()


def test_cancel_preserved(runner):
    args, configs, failures, executor, _ = runner
    failures[:] = [main.RunStopped("stopped")]
    with pytest.raises(main.RunStopped):
        asyncio.run(main.run_korea_toss(args, execute=False))
    assert len(configs) == 1 and not fallback._EXHAUSTED
    executor.assert_not_awaited()


def test_setting_is_boolean_and_cli_can_disable(runner):
    args, _, _, _, _ = runner
    assert args.openai_credit_fallback is True
    assert main.make_parser().parse_args(["--no-openai-credit-fallback"]).openai_credit_fallback is False
    assert main.save_trading_settings({"openai_credit_fallback": False})["openai_credit_fallback"] is False
    for value in ("false", "true", 0, 1, None):
        with pytest.raises(ValueError):
            main.validate_trading_settings({"openai_credit_fallback": value})


def test_latch_scope_does_not_leak_or_reuse_changed_key(runner, monkeypatch):
    args, configs, failures, executor, _ = runner
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-one")
    asyncio.run(main.run_korea_toss(args, execute=False))
    assert "test-key-one" not in repr(fallback._EXHAUSTED)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-two")
    asyncio.run(main.run_korea_toss(args, execute=False))
    assert configs[-1]["llm_provider"] == "hybrid"
