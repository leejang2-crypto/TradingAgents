from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

import main
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.dataflows.config import set_config
from tradingagents.llm_clients.routing import describe_routing
from tradingagents.agents.utils.local_news import render_verified_news


@pytest.fixture
def settings_args(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    args = main.make_parser().parse_args([])
    main.initialize_trading_settings(args)
    return args


@pytest.mark.parametrize("key,value", [
    ("paper_capital", "NaN"), ("paper_capital", "0.1"),
    ("daily_target_return", "Infinity"), ("position_pct", "invalid"),
    ("llm_timeout", "NaN"), ("llm_timeout", "3601"),
    ("trade_candidates", 2.5), ("news_display", 101),
    ("provider", "unknown"), ("provider", []),
    ("ollama_quick_model", ""), ("ollama_base_url", "https://example.com/v1"),
    ("ollama_base_url", "http://localhost:11434/api"),
    ("hybrid_agent_providers", {"news_analyst": "unknown"}),
    ("hybrid_agent_providers", {"news_analyst": []}),
])
def test_invalid_settings_are_rejected_without_replacing_file(settings_args, key, value):
    before = main.settings_path().read_bytes()
    with pytest.raises(ValueError):
        main.save_trading_settings({key: value})
    assert main.settings_path().read_bytes() == before


def test_hybrid_roundtrip_routes_only_news_locally(settings_args):
    main.save_trading_settings({"provider": "hybrid", "ollama_quick_model": "qwen3:8b"})
    main.apply_trading_settings(settings_args)
    config = main.make_config(settings_args, korea_mode=True, minimal_mode=False)
    routes = describe_routing(config)
    assert config["news_local_single_pass"] is True
    assert routes["news_analyst"] == {"provider": "ollama", "model": "qwen3:8b"}
    assert routes["market_analyst"]["provider"] == "openai"
    assert routes["portfolio_manager"]["model"] == "gpt-5.5"


def test_provider_switch_does_not_reuse_wrong_model_pool(settings_args):
    main.save_trading_settings({"provider": "ollama"})
    main.apply_trading_settings(settings_args)
    assert main.resolve_models(settings_args) == ("qwen3:8b", "gemma3:12b", "http://localhost:11434/v1")
    main.save_trading_settings({"provider": "openai"})
    main.apply_trading_settings(settings_args)
    config = main.make_config(settings_args, korea_mode=True, minimal_mode=False)
    assert config["news_local_single_pass"] is False
    assert main.resolve_models(settings_args) == ("gpt-5.5", "gpt-5.5", None)


def test_explicit_cli_wins_then_web_can_change_model(settings_args):
    main.save_trading_settings({"provider": "ollama"})
    args = main.make_parser().parse_args(["--provider", "openai", "--model", "gpt-5.4-mini"])
    args._explicit_settings = {"provider"}
    main.initialize_trading_settings(args)
    assert args.provider == "openai"
    assert main.resolve_models(args)[0] == "gpt-5.4-mini"
    main.save_trading_settings({"openai_quick_model": "gpt-5.5"})
    main.apply_trading_settings(args)
    assert main.resolve_models(args)[0] == "gpt-5.5"


def test_live_launcher_reads_web_settings_without_overwriting_them(settings_args, monkeypatch):
    monkeypatch.setattr(main, "PositionRiskManager", Mock())
    main.save_trading_settings({"provider": "hybrid", "openai_quick_model": "gpt-5.4-mini"})
    saved_bytes = main.settings_path().read_bytes()
    args = main.make_parser().parse_args([
        "--scenario", "korea-execute", "--yes", "--max-order-amount", "750000",
    ])
    args.monitor = False
    args._explicit_settings = {"scenario", "yes", "max_order_amount"}

    async def inspect_config(actual):
        assert actual.provider == "hybrid"
        assert main.resolve_models(actual)[0] == "gpt-5.4-mini"
        assert main.order_budget(actual) == Decimal("750000")
        return 0

    monkeypatch.setattr(main, "run_scenarios", inspect_config)
    assert asyncio.run(main._main_async(args)) == 0
    assert main.settings_path().read_bytes() == saved_bytes


def test_live_explicit_provider_overrides_saved_provider(settings_args, monkeypatch):
    monkeypatch.setattr(main, "PositionRiskManager", Mock())
    main.save_trading_settings({"provider": "ollama"})
    args = main.make_parser().parse_args(["--scenario", "korea-execute", "--yes", "--provider", "openai"])
    args.monitor = False
    args._explicit_settings = {"provider"}

    async def inspect_config(actual):
        assert actual.provider == "openai"
        return 0

    monkeypatch.setattr(main, "run_scenarios", inspect_config)
    assert asyncio.run(main._main_async(args)) == 0


def test_concurrent_partial_settings_updates_are_not_lost(settings_args):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(main.save_trading_settings, [
            {"paper_capital": "12000000"}, {"trade_candidates": 2},
        ]))
    assert len(results) == 2
    saved = main.load_trading_settings()
    assert saved["paper_capital"] == "12000000"
    assert saved["trade_candidates"] == 2


def test_corrupt_settings_and_scheduler_do_not_reset(settings_args):
    main.settings_path().write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        main.initialize_trading_settings(settings_args)
    main.atomic_write_json(main.scheduler_state_path(), [])
    with pytest.raises(ValueError):
        main.has_scheduler_completed("2026-09-10")


def test_second_runner_is_rejected(settings_args):
    with main.scheduler_process_lock(settings_args):
        with pytest.raises(RuntimeError, match="already running"):
            with main.scheduler_process_lock(settings_args):
                pass


def test_local_news_is_one_tool_free_llm_call(monkeypatch):
    set_config({"news_local_single_pass": True})
    tool = Mock()
    tool.invoke.return_value = "2026-09-09: Reported earnings; https://example.org/news"
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_news", tool)
    llm = Mock()
    llm.invoke.return_value = AIMessage(content='{"ticker":"005930.KS","items":[{"article":1,"excerpt":1,"signal":"catalyst"}]}')
    state = {"trade_date": "2026-09-10", "company_of_interest": "005930.KS"}
    result = create_news_analyst(llm)(state)
    assert "Reported earnings" in result["news_report"]
    llm.invoke.assert_called_once()
    llm.bind_tools.assert_not_called()
    tool.invoke.assert_called_once_with({"ticker": "005930.KS", "start_date": "2026-09-03", "end_date": "2026-09-10"})
    assert "https://example.org/news" in llm.invoke.call_args.args[0][1].content
    assert '"excerpt":1' in llm.invoke.call_args.args[0][0].content
    assert result["messages"][0].content == result["news_report"]


def test_local_news_failure_does_not_silently_call_cloud(monkeypatch):
    set_config({"news_local_single_pass": True})
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_news", Mock(invoke=Mock(return_value="Reported earnings today.")))
    llm = Mock(invoke=Mock(side_effect=TimeoutError("local timeout")))
    with pytest.raises(TimeoutError):
        create_news_analyst(llm)({"trade_date": "2026-09-10", "company_of_interest": "005930.KS"})
    llm.invoke.assert_called_once()


@pytest.mark.parametrize("response", [
    '{"ticker":"005930.KS","items":[{"article":1,"excerpt":99,"signal":"risk"}]}',
    '{"ticker":"005930.KS","items":[{"article":1,"quote":"Reported earnings today.","signal":"risk"}]}',
])
def test_local_news_invalid_selection_does_not_retry(monkeypatch, response):
    set_config({"news_local_single_pass": True})
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_news", Mock(invoke=Mock(return_value="Reported earnings today.")))
    llm = Mock(invoke=Mock(return_value=AIMessage(content=response)))
    with pytest.raises(ValueError, match="unknown excerpt"):
        create_news_analyst(llm)({"trade_date": "2026-09-10", "company_of_interest": "005930.KS"})
    llm.invoke.assert_called_once()
    llm.bind_tools.assert_not_called()


def test_local_news_empty_sources_do_not_call_llm(monkeypatch):
    set_config({"news_local_single_pass": True})
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_news", Mock(invoke=Mock(return_value="")))
    llm = Mock()
    with pytest.raises(ValueError, match="no usable"):
        create_news_analyst(llm)({"trade_date": "2026-09-10", "company_of_interest": "005930.KS"})
    llm.invoke.assert_not_called()


def test_local_news_rejects_invented_company_and_wrong_source():
    sources = ["005930.KS announces an earnings date.", "Another article with no financial data."]
    invented = '{"ticker":"005930.KS","items":[{"article":1,"quote":"005930.KS is Hanwha S1","signal":"catalyst"}]}'
    with pytest.raises(ValueError, match="absent"):
        render_verified_news(invented, "005930.KS", sources)
    wrong_source = '{"ticker":"005930.KS","items":[{"article":2,"quote":"announces an earnings date","signal":"catalyst"}]}'
    with pytest.raises(ValueError, match="absent"):
        render_verified_news(wrong_source, "005930.KS", sources)


def test_unverified_model_fields_are_never_published():
    report = render_verified_news(
        '{"ticker":"005930.KS","company":"invented-name","items":[{"article":1,"quote":"announces an earnings date","signal":"catalyst","explanation":"invented-story"}]}',
        "005930.KS", ["005930.KS announces an earnings date."],
    )
    assert "invented" not in report


def test_dry_run_is_recorded_not_claimed_as_filled(tmp_path):
    plan = plan_from_decision("**Rating**: Buy", "005930.KS", max_order_amount=Decimal("1000000"))
    result = asyncio.run(execute_plan(plan, paper_trade_log=tmp_path / "trades.jsonl"))
    assert result["recorded"] and not result["executed"]
    assert result["paperTrade"]["record"]["status"] == "RECORDED"


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity"])
def test_invalid_order_budgets_never_fall_back_to_default(budget):
    with pytest.raises(ValueError):
        plan_from_decision("**Rating**: Buy", "005930.KS", max_order_amount=Decimal(budget))


def test_order_is_not_recorded_after_market_close(settings_args, monkeypatch):
    args = settings_args
    args.date = "2026-09-10"
    fake_graph = Mock()
    fake_graph.propagate.return_value = ({"final_trade_decision": "**Rating**: Buy"}, "Buy")
    fake_graph.save_reports.return_value = "report.md"
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=fake_graph))
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: False)
    execute = Mock(side_effect=AssertionError("must not record order"))
    monkeypatch.setattr(main, "execute_plan", execute)
    result = asyncio.run(main.run_korea_toss(args, execute=False))
    assert result["toss"]["skipped"] == "market-closed"
    execute.assert_not_called()


def test_scheduler_records_errors_and_does_not_rerun_same_day(settings_args, monkeypatch):
    class MarketTime(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 17, 10, tzinfo=main.KOREA_TZ).astimezone(tz)

    monkeypatch.setattr(main, "datetime", MarketTime)
    calls = []
    class StopLoop(Exception):
        pass

    async def run(args):
        calls.append(args.date)
        return {"ok": False, "summaryPath": "failed.json"}

    async def stop(_):
        raise StopLoop

    monkeypatch.setattr(main, "run_auto_paper", run)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopLoop):
        asyncio.run(main.run_daily_market_scheduler(settings_args))
    assert len(calls) == 1
    assert main.load_scheduler_state()["runs"][calls[0]]["status"] == "completed-with-errors"
    assert main.build_monitor_snapshot()["runtime"]["status"] == "completed-with-errors"


def test_dashboard_has_model_controls_and_preserves_form_on_refresh(settings_args):
    html = main.render_monitor_html(main.build_monitor_snapshot())
    assert 'name="provider"' in html
    assert 'name="route_news_analyst"' in html
    assert 'name="ollama_quick_model"' in html
    assert 'data-live="analyses"' in html
    assert "setInterval(refreshStatus, 30000)" in html
    assert "location.reload" not in html
