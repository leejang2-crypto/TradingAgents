"""Offline regression checks for selected upstream safety backports."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph import MessagesState
from langgraph.prebuilt import ToolNode

import main
from tradingagents.agents.utils.rating import parse_rating, run_rating
from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.dataflows.config import get_config, run_config, set_config
from tradingagents.dataflows import stockstats_utils as ohlcv
from tradingagents.agents.utils import core_stock_tools as prices
from tradingagents.agents.utils import news_data_tools as news
from tradingagents.agents.utils.experience import ExperienceStore


@pytest.mark.parametrize("text,expected", [
    ("Consensus rating: Buy\nFinal Rating: Sell", "Sell"),
    ("Trader's rating: Sell\n**Rating**: Buy", "Buy"),
    ("This is not a Sell recommendation.", "REVIEW"),
    ("No usable data", "REVIEW"),
    ("Consensus rating: Buy", "REVIEW"),
    ("Rating Scale: Buy, Hold, Sell", "REVIEW"),
    ("Rating: Buy / Sell", "REVIEW"),
    ("Rating: Buy\nRating: Sell", "REVIEW"),
    ("Rating: unknown\nRating: Buy", "REVIEW"),
    ("> Rating: Buy", "REVIEW"),
    ("```\nRating: Buy\n```", "REVIEW"),
    ("Rating\uff1a Hold", "Hold"),
    ("- **Final Rating**: **Overweight**", "Overweight"),
])
def test_rating_integrity(text, expected):
    assert parse_rating(text) == expected
    plan = plan_from_decision(text, "005930.KS")
    assert plan.rating == expected
    if expected == "REVIEW":
        assert plan.action == "REVIEW"


def test_typed_rating_wins_over_quoted_text():
    state = {"final_rating": "Sell", "final_trade_decision": "Rating: Buy"}
    assert run_rating(state) == "Sell"
    assert plan_from_decision(state["final_trade_decision"], "005930.KS",
                              final_rating=run_rating(state)).action == "SELL"
    assert run_rating({**state, "final_rating": "invalid"}) == "REVIEW"


@pytest.mark.parametrize("live", [False, True])
def test_review_never_calls_execution_backend(live, tmp_path):
    plan = plan_from_decision("No decision", "005930.KS")
    result = asyncio.run(execute_plan(plan, execute=live, paper_trade_log=tmp_path / "orders"))
    assert result["skipped"] == "decision-review"
    assert not list(tmp_path.iterdir())
    manager = object.__new__(PositionRiskManager)
    manager.live = live
    # No store/backend attributes: any accidental access fails this test.
    assert manager.execute(plan)["skipped"] == "decision-review"


@pytest.mark.parametrize("live", [False, True])
def test_runner_records_review_without_order_attempt(live, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    graph = Mock()
    graph.propagate.return_value = ({"news_report": "Evidence", "final_trade_decision": "Ambiguous",
                                     "final_rating": "REVIEW"}, "REVIEW")
    graph.save_reports.return_value = tmp_path / "report.md"
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    executor = Mock(side_effect=AssertionError("Must not order"))
    monkeypatch.setattr(main, "execute_plan", executor)
    args = main.make_parser().parse_args(["--scenario", "korea-execute" if live else "korea-dry-run",
                                         "--yes", "--date", "2026-09-01"])
    result = asyncio.run(main.run_korea_toss(args, execute=live))
    assert result["decision"] == "REVIEW" and result["ok"] is False
    executor.assert_not_called()
    saved = ExperienceStore(tmp_path / "learning/experiences.sqlite3").get(result["learning"]["runId"])
    assert "ORDER_ATTEMPT" not in [e["kind"] for e in saved["events"]]
    assert next(e for e in saved["events"] if e["kind"] == "ANALYSIS")["payload"]["finalRating"] == "REVIEW"


def test_run_scope_isolated_and_restored_across_threads():
    set_config({"scope_test": "global"})
    barrier = Barrier(2)

    def work(value):
        with run_config({"scope_test": value}, value, "2026-10-15"):
            barrier.wait(timeout=5)
            assert get_config()["scope_test"] == value
            assert get_config()["analysis_ticker"] == value
            with run_config({"scope_test": "nested"}):
                assert get_config()["scope_test"] == "nested"
            assert get_config()["scope_test"] == value
        assert get_config()["scope_test"] == "global"

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(work, ["005930.KS", "000660.KS"]))


def test_tools_bind_symbol_and_date_through_langgraph(monkeypatch):
    called = Mock(return_value="data")
    monkeypatch.setattr(prices, "route_to_vendor", called)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([prices.get_stock_data]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    with run_config({}, "005930.KS", "2026-10-15"):
        app.invoke({"messages": [AIMessage(content="", tool_calls=[{
            "name": "get_stock_data", "id": "one", "args": {
                "symbol": "WRONG", "start_date": "2026-10-01", "end_date": "2099-01-01",
            }}])]})
    called.assert_called_once_with("get_stock_data", "005930.KS", "2026-10-01", "2026-10-15")


@pytest.mark.parametrize("start", ["2026-10-16", "bad date"])
def test_invalid_window_never_queries_vendor(start, monkeypatch):
    called = Mock()
    monkeypatch.setattr(news, "route_to_vendor", called)
    with run_config({}, "005930.KS", "2026-10-15"):
        news.get_news.invoke({"ticker": "WRONG", "start_date": start, "end_date": "2099-01-01"})
    called.assert_not_called()


def test_local_news_direct_tool_call_uses_bound_catalog_ticker(monkeypatch):
    called = Mock(return_value="news")
    monkeypatch.setattr(news, "route_to_vendor", called)
    with run_config({}, "005930.KS", "2026-10-15"):
        news.get_news.invoke({"ticker": "WRONG", "start_date": "2026-10-01", "end_date": "2099-01-01"})
    called.assert_called_once_with("get_news", "005930.KS", "2026-10-01", "2026-10-15")


def test_same_day_cache_refresh_and_historical_reuse(monkeypatch, tmp_path):
    today = pd.Timestamp.today().normalize()
    frame = pd.DataFrame({"Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [10]},
                         index=pd.DatetimeIndex([today], name="Date"))
    download = Mock(return_value=frame)
    monkeypatch.setattr(ohlcv.yf, "download", download)
    with run_config({"data_cache_dir": str(tmp_path)}):
        day = today.strftime("%Y-%m-%d")
        assert ohlcv.load_ohlcv("005930.KS", day)["Close"].iloc[-1] == 100
        path = next(tmp_path.glob("*.csv"))
        ohlcv.load_ohlcv("005930.KS", day)
        assert download.call_count == 1
        os.utime(path, (0, 0))
        assert not ohlcv._needs_same_day_refresh(path, today - pd.Timedelta(days=1), today)
        updated = frame.copy()
        updated["Close"] = 110
        download.return_value = updated
        assert ohlcv.load_ohlcv("005930.KS", day)["Close"].iloc[-1] == 110
        assert download.call_count == 2


def test_failed_atomic_write_keeps_previous_cache(monkeypatch, tmp_path):
    path = tmp_path / "cache.csv"
    frame = pd.DataFrame({"Close": [100]})
    ohlcv._write_cache(frame, str(path))
    before = path.read_bytes()
    monkeypatch.setattr(ohlcv.os, "replace", Mock(side_effect=OSError("disk error")))
    with pytest.raises(OSError):
        ohlcv._write_cache(pd.DataFrame({"Close": [200]}), str(path))
    assert path.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 1


def test_trader_receives_market_report(monkeypatch):
    from tradingagents.agents.trader import trader
    monkeypatch.setattr(trader, "get_instrument_context_from_state", lambda state: "Samsung")
    model = Mock()
    model.with_structured_output.side_effect = NotImplementedError
    model.invoke.return_value = AIMessage(content="Hold")
    trader.create_trader(model)({"company_of_interest": "005930.KS", "investment_plan": "Plan",
                                "market_report": "ATR 123; close 456"})
    prompt = str(model.invoke.call_args.args[0])
    assert "ATR 123; close 456" in prompt
    assert "Do not invent unavailable levels" in prompt


@pytest.mark.parametrize("structured", [True, False])
def test_portfolio_manager_preserves_typed_rating(structured, monkeypatch):
    from tradingagents.agents.managers import portfolio_manager as pm
    monkeypatch.setattr(pm, "get_instrument_context_from_state", lambda state: "Samsung")
    llm = Mock()
    if structured:
        llm.with_structured_output.return_value.invoke.return_value = {
            "rating": "Sell", "executive_summary": "Exit",
            "investment_thesis": "Other reasoning\nRating: Buy",
        }
    else:
        llm.with_structured_output.side_effect = NotImplementedError
        llm.invoke.return_value = AIMessage(content="Consensus rating: Buy\nFinal Rating: Sell")
    risk = {key: "" for key in ["history", "aggressive_history", "conservative_history", "neutral_history",
                                "current_aggressive_response", "current_conservative_response", "current_neutral_response"]}
    risk["count"] = 1
    result = pm.create_portfolio_manager(llm)({"risk_debate_state": risk,
        "investment_plan": "Plan", "trader_investment_plan": "Proposal"})
    assert result["final_rating"] == "Sell"
    assert run_rating(result) == "Sell"
    if structured:
        llm.invoke.assert_not_called()


def test_local_validation_blocks_typed_buy_as_review():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"llm_provider": "hybrid"}
    state = {"market_report": "ticker 000660", "final_trade_decision": "Rating: Buy",
             "final_rating": "Buy"}
    graph._apply_local_safe_validation(state, "005930.KS")
    assert state["final_rating"] == "REVIEW"
    assert "REVIEW" in state["final_trade_decision"]


def test_review_excluded_from_learning_and_pending_settlement(tmp_path):
    from tradingagents.agents.utils.memory import TradingMemoryLog
    memory = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    memory.store_decision("005930.KS", "2026-10-01", "No decision", final_rating="REVIEW")
    assert memory.load_entries()[0]["rating"] == "REVIEW"
    assert memory.get_pending_entries() == []
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    kwargs = {"mode": "dry-run", "ticker": "005930.KS", "objective": {}, "routing": {}}
    first = store.start(trade_date="2026-10-01", **kwargs)
    store.save_analysis(first, {"news_report": "An untrusted prior analysis", "final_rating": "REVIEW"})
    second = store.start(trade_date="2026-10-02", **kwargs)
    assert store.contexts(second) == {}
