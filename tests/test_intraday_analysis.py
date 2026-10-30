import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import io
import json
from unittest.mock import Mock, AsyncMock

import pytest
import main
from tests.test_intraday import data
from tests.test_twice_daily_schedule import clock, StopScheduler
from tests.test_position_risk import env
from tradingagents.brokers import intraday_analysis as fast
from tradingagents.brokers.intraday import evaluate_entry, INTRADAY_DEFAULTS, bars, relative_volume
from tradingagents.brokers.candidate_screen import measure_candidate
from tradingagents.llm_clients.capabilities import get_capabilities


@pytest.fixture
def config():
    args = main.make_parser().parse_args(["--provider", "ollama"])
    cfg = main.make_config(args, korea_mode=True, minimal_mode=False)
    cfg["intraday_fast"] = True
    return cfg


@pytest.fixture
def technical(data):
    rows, book, markets, start, now = deepcopy(data)
    settings = {**INTRADAY_DEFAULTS, "min_session_turnover": "1000000", "per_trade_budget": "1000000"}
    signal = evaluate_entry("005930", rows, book, markets, start, now, settings)
    assert signal["allowed"]
    return {"ticker": "005930.KS", "date": now.date().isoformat(), "asOf": now.isoformat(),
            "start": start.isoformat(), "signal": signal, "candles": rows, "markets": markets,
            "orderbook": book, "settings": settings}


@pytest.fixture
def material(technical):
    return {"ticker": technical["ticker"], "date": technical["date"], "items": [
        {"id": 1, "published": technical["asOf"], "text": "Source excerpt with more than ten characters", "url": "https://example.com/news"}]}


@pytest.fixture(autouse=True)
def empty_cache():
    fast._CACHE.clear()


def local_response(monkeypatch, material, **overrides):
    content = {"ticker": material["ticker"], "date": material["date"],
               "items": [{"id": 1, "signal": "catalyst"}]}
    content.update(overrides)
    calls = []
    def invoke(request, timeout):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"done": True, "done_reason": "stop", "message": {"content": json.dumps(content)}}).encode())
    monkeypatch.setattr(fast, "urlopen", invoke)
    return calls


def test_single_local_model_bounded_tool_free_and_cached(monkeypatch, material, config):
    calls = local_response(monkeypatch, material)
    first = fast.review_news(material, config)
    assert not first["cacheHit"] and first["model"] == "qwen3:8b"
    assert fast.review_news(material, config)["cacheHit"]
    assert len(calls) == 1 and "tools" not in calls[0]
    assert calls[0]["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 600}
    material["items"][0]["text"] += " changed"
    assert not fast.review_news(material, config)["cacheHit"]
    assert len(calls) == 2


@pytest.mark.parametrize("change", [
    {"ticker": "000660.KS"}, {"date": "2024-06-30"}, {"items": []},
    {"items": [{"id": 2, "signal": "catalyst"}]},
    {"items": [{"id": True, "signal": "catalyst"}]},
    {"items": [{"id": 1, "signal": "guaranteed_profit"}]},
])
def test_wrong_or_invented_evidence_never_validates(monkeypatch, material, config, change):
    local_response(monkeypatch, material, **change)
    with pytest.raises(ValueError):
        fast.review_news(material, config)
    assert not fast._CACHE


def test_incomplete_output_and_oversized_prompt_block(monkeypatch, material, config):
    monkeypatch.setattr(fast, "urlopen", lambda *a, **k: io.BytesIO(b'{"done":true,"done_reason":"length"}'))
    with pytest.raises(ValueError, match="incomplete"):
        fast.review_news(material, config)
    material["items"][0]["text"] = "x" * 7000
    with pytest.raises(ValueError, match="budget"):
        fast.review_news(material, config)


@pytest.mark.parametrize("signal,expected", [("catalyst", "Buy"), ("neutral", "Buy"), ("risk", "Hold"), ("uncertainty", "REVIEW")])
def test_deterministic_contract_and_news_veto(monkeypatch, config, technical, material, signal, expected):
    local_response(monkeypatch, material, items=[{"id": 1, "signal": signal}])
    review = fast.review_news(material, config)
    state = fast.build_state(technical["ticker"], technical["date"], config, technical, review, material)
    assert state["final_rating"] == expected
    contract = state["intraday_contract"]
    assert contract["horizon"] == "same-day"
    assert Decimal(contract["target"]) / Decimal(contract["entry"]) - 1 == Decimal(".01")
    assert "당일 청산" in state["final_trade_decision"]
    technical["signal"].update(allowed=False, reasons=["volume insufficient"])
    assert fast.build_state(technical["ticker"], technical["date"], config, technical, review, material)["final_rating"] == "Hold"


def test_missing_news_or_invalid_response_is_review(config, technical):
    assert fast.build_state(technical["ticker"], technical["date"], config, technical)["final_rating"] == "REVIEW"
    assert fast.build_state(technical["ticker"], technical["date"], config, technical, error="bad JSON")["final_rating"] == "REVIEW"


@pytest.mark.parametrize("field,value", [("ticker", "000660.KS"), ("date", "2024-06-30"), ("asOf", "2026-09-15T09:25:00+09:00")])
def test_wrong_or_stale_market_contract_is_rejected(config, technical, field, value):
    technical[field] = value
    with pytest.raises(ValueError):
        fast.build_state("005930.KS", "2026-09-15", config, technical)


def test_news_is_dated_no_future_input(tmp_path, material, technical):
    path = tmp_path / "news.json"
    now = fast.stamp(technical["asOf"])
    main.atomic_write_json(path, {"tickers": [material["ticker"]], "endDate": material["date"], "articles": [
        {"title": "Current news excerpt", "published_at": (now - timedelta(hours=1)).isoformat()},
        {"title": "Future news excerpt", "published_at": (now + timedelta(minutes=1)).isoformat()},
        {"title": "Old news excerpt", "published_at": (now - timedelta(days=2)).isoformat()},
    ]})
    result = fast.news_material({"ok": True, "path": str(path)}, material["ticker"], material["date"], now)
    assert result["availableCount"] == 1 and result["items"][0]["text"] == "Current news excerpt"


def test_replay_round_trip_preserves_raw_inputs(tmp_path, config, technical):
    config["results_dir"] = str(tmp_path)
    state = fast.build_state(technical["ticker"], technical["date"], config, technical)
    path = fast.IntradayReport(config).save_reports(state, technical["ticker"])
    saved = json.loads(path.with_name("intraday_replay.json").read_text())
    raw = saved["technical"]
    recomputed = evaluate_entry("005930", raw["candles"], raw["orderbook"], raw["markets"],
                               fast.stamp(raw["start"]), fast.stamp(raw["asOf"]), raw["settings"])
    assert recomputed == technical["signal"]


def test_opening_volume_excluded_but_final_threshold_preserved(data):
    rows, book, markets, start, now = deepcopy(data)
    for row in rows[:5]:
        row["volume"] = "100000"
    five, _, _ = bars(rows, start, now)
    assert relative_volume(five) == 5
    for row in rows[-5:]:
        row["volume"] = "500"
    check = measure_candidate(rows, start, now, 0, {"min_session_turnover": "1"})
    assert check["watchable"] and not check["eligible"] and check["state"] == "WATCH"
    signal = evaluate_entry("005930", rows, book, markets, start, now, {"min_session_turnover": "1"})
    assert not signal["allowed"]


def test_unaffordable_candidate_excluded_before_llm(data):
    rows, _, _, start, now = data
    result = measure_candidate(rows, start, now, 0, {"min_session_turnover": "1", "per_trade_budget": "10000"})
    assert not result["eligible"] and not result["watchable"]
    assert any("1주" in reason for reason in result["reasons"])


@pytest.mark.parametrize("scenario", ["auto-paper", "auto-live"])
def test_five_minute_scheduler_and_restart_do_not_repeat(clock, monkeypatch, scenario):
    clock.current = clock.current.replace(minute=20)
    args = main.make_parser().parse_args(["--scenario", scenario, "--yes", "--buy-start-minutes", "20",
                                        "--candidate-scan-minutes", "5"])
    calls = []
    async def run(args, *, execute):
        assert main.has_scheduler_completed(args.date, scenario, args._schedule_slot)
        calls.append(args._schedule_slot)
        return {"ok": True}
    async def advance(_):
        if clock.current.minute == 20:
            clock.current += timedelta(minutes=5)
        else:
            raise StopScheduler
    monkeypatch.setattr(main, "run_auto_trading", run)
    monkeypatch.setattr(main.asyncio, "sleep", advance)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    assert calls == ["scan-0920", "scan-0925"]


def test_five_minute_schedule_stops_new_entries_at_cutoff(clock, monkeypatch):
    clock.current = clock.current.replace(hour=15)
    args = main.make_parser().parse_args([])
    run = AsyncMock()
    monkeypatch.setattr(main, "run_auto_trading", run)
    async def stop(_):
        raise StopScheduler
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    run.assert_not_called()


def test_cached_shortlist_does_not_cache_market_screen(clock, monkeypatch, tmp_path):
    args = main.make_parser().parse_args(["--date", "2026-09-10"])
    collect = Mock(return_value={"ok": True, "candidates": [{"ticker": "005930.KS"}]})
    monkeypatch.setattr(main, "collect_candidate_snapshot", collect)
    first = main.discovery_for_scan(args)
    first["candidates"][0]["intraday"] = {"eligible": False}
    clock.current += timedelta(minutes=5)
    second = main.discovery_for_scan(args)
    assert "intraday" not in second["candidates"][0] and collect.call_count == 1
    args.candidate_days += 1
    main.discovery_for_scan(args)
    assert collect.call_count == 2
    clock.current += timedelta(minutes=15)
    main.discovery_for_scan(args)
    assert collect.call_count == 3


def test_gemma_does_not_bind_function_tools_for_structured_output():
    assert get_capabilities("gemma3:12b").preferred_structured_method == "json_schema"


@pytest.mark.parametrize("metadata,valid", [({"status": "completed"}, True), ({"finish_reason": "stop"}, True),
    ({"status": "incomplete"}, False), ({"finish_reason": "length"}, False), ({}, False)])
def test_cloud_completion_metadata(monkeypatch, material, config, metadata, valid):
    from langchain_core.messages import AIMessage
    config["llm_provider"] = "openai"
    config["quick_think_llm"] = "gpt-5.5"
    client = Mock()
    client.get_llm.return_value.invoke.return_value = AIMessage(content=json.dumps({
        "ticker": material["ticker"], "date": material["date"], "items": [{"id": 1, "signal": "neutral"}]}),
        response_metadata=metadata)
    monkeypatch.setattr(fast, "create_llm_client", lambda *a, **kw: client)
    if valid:
        assert fast.review_news(material, config)["provider"] == "openai"
    else:
        with pytest.raises(ValueError, match="incomplete"):
            fast.review_news(material, config)


def test_english_price_cannot_bypass_legacy_stop_validation():
    from tradingagents.agents.utils.local_safe import validate_trading_state
    state = {"market_report": "Latest close: 1,334,000 KRW", "trader_investment_plan": "Stop-loss: 128463",
             "final_trade_decision": "Hold. " + "Source-backed report. " * 10, "final_rating": "Hold"}
    result = validate_trading_state(state, "009150.KS")
    assert not result.ok and any("implausible" in reason for reason in result.reasons)


def test_account_exclusions_are_read_only_and_avoid_repeat_candidates(env):
    manager, broker, times, _ = env
    broker.positions["005930"] = Decimal(2)
    with manager.store.locked():
        state = manager.store.load()
        state["positions"]["000660"] = {"quantity": "0", "closedAt": times[0].isoformat()}
        manager.store.save(state)
    exclusions = manager.candidate_exclusions(["005930", "000660", "009150"])
    assert set(exclusions) == {"005930", "000660"}
    assert not broker.orders and broker.positions["005930"] == 2


def test_collect_technical_preserves_inputs_and_never_submits(data):
    rows, book, markets, start, now = deepcopy(data)
    class Backend:
        clock = staticmethod(lambda: now)
        def calendar(self, day):
            return {"today": {"date": day, "integrated": {"regularMarket": {
                "startTime": start.isoformat(), "endTime": start.replace(hour=15, minute=30).isoformat()}}}}
        def call(self, method, symbol, **kwargs):
            if method == "get_orderbook":
                return book
            assert method == "get_candles"
            candles = deepcopy(markets.get(symbol, rows))
            if symbol in markets:
                for row in candles:
                    row["timestamp"] = (fast.stamp(row["timestamp"]) - timedelta(minutes=1)).isoformat()
            return {"candles": candles, "nextBefore": None}
    settings = {**INTRADAY_DEFAULTS, "market_open": "09:00", "market_close": "15:30", "buy_cutoff_minutes": 30, "buy_start_minutes": 20,
                "min_session_turnover": "1000000", "per_trade_budget": "1000000"}
    result = fast.collect_technical(Backend(), "005930.KS", now.date().isoformat(), settings)
    assert result["signal"]["allowed"] and result["candles"] == rows and result["orderbook"] == book
    settings["per_trade_budget"] = "10000"
    assert not fast.collect_technical(Backend(), "005930.KS", now.date().isoformat(), settings)["signal"]["allowed"]


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("analysis_only", [False, True])
def test_fast_main_path_skips_graph_and_rechecks_risk(tmp_path, monkeypatch, technical, material, execute, analysis_only):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    monkeypatch.setattr(main, "today_korea", lambda: fast.stamp(technical["asOf"]).date())
    args = main.make_parser().parse_args(["--date", technical["date"], "--yes", "--run-mode", "once"])
    args._intraday_fast = True
    args._analysis_only = analysis_only
    manager = object.__new__(main.PositionRiskManager)
    manager.backend = Mock()
    manager.store = Mock(path=tmp_path / "positions" / ("live-account.sqlite3" if execute else "paper.sqlite3"))
    manager.entry_watch = Mock()
    manager.execute = Mock(return_value={"ok": True, "executed": False, "skipped": "intraday-filter"})
    manager.execute_with_watch = Mock(side_effect=AssertionError("No stale entry watches"))
    args._position_manager = manager
    monkeypatch.setattr(main, "collect_technical", lambda *a, **kw: technical)
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda *a: {"ok": True, "path": "fixture"})
    monkeypatch.setattr(main, "news_material", lambda *a: material)
    review = {"ticker": material["ticker"], "date": material["date"], "items": [{"id": 1, "signal": "neutral"}]}
    monkeypatch.setattr(main, "review_news", lambda *a: review)
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(side_effect=AssertionError("No graph on fast path")))
    monkeypatch.setattr(main, "execute_plan", AsyncMock(side_effect=AssertionError("No direct execution")))
    result = asyncio.run(main.run_korea_toss(args, execute=execute))
    assert result["analysisEngine"] == "rules-news"
    assert result["learning"]["kind"] == "audit-only"
    assert result["toss"]["skipped"] == ("analysis-only" if analysis_only else "intraday-filter")
    if analysis_only:
        manager.execute.assert_not_called()
    else:
        assert manager.execute.call_args.args[0].action == "BUY"
        manager.execute.assert_called_once()
    assert set(result["agentRouting"]) == {"news_analyst"}
