from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from tradingagents.brokers.intraday import bars
from tradingagents.brokers.paper_account import PaperAccount
from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.brokers.toss_executor import TossOrderPlan
from tradingagents.research import strategy_comparison as research
from tests.test_intraday import data
from tests import test_dashboard_auth
from tests.test_dashboard_auth import login, request

server = test_dashboard_auth.server


@pytest.fixture
def technical():
    rows, book, markets, start, now = data.__wrapped__()
    for row in rows[-5:]:
        row["volume"] = "2000"
    for minute in range(21, 26):
        rows.append({"timestamp": (start + timedelta(minutes=minute)).isoformat(), "currency": "KRW",
                     "openPrice": "10020", "highPrice": "10050", "lowPrice": "10010", "closePrice": "10040", "volume": "4000"})
    now = start + timedelta(minutes=25, seconds=5)
    book.update(timestamp=now.isoformat(), asks=[{"price": "10040", "volume": "100"}], bids=[{"price": "10030", "volume": "100"}])
    return {"ticker": "005930.KS", "date": str(start.date()), "start": start.isoformat(), "asOf": now.isoformat(),
            "end": (start + timedelta(hours=6, minutes=30)).isoformat(), "candles": rows,
            "orderbook": book, "markets": {key: deepcopy(rows) for key in markets},
            "settings": {"buy_start_minutes": 20, "min_session_turnover": "1000000", "min_relative_volume": "1.2"}}


def historical(t, count=10):
    start = research.stamp(t["start"])
    result = []
    for index in range(1, count + 1):
        shift = timedelta(days=index)
        rows = deepcopy(t["candles"])
        for row in rows:
            row["timestamp"] = (research.stamp(row["timestamp"]) - shift).isoformat()
            row["volume"] = "2000"
        result.append({"symbol": "005930", "date": str((start - shift).date()), "start": (start - shift).isoformat(),
                       "end": (research.stamp(t["end"]) - shift).isoformat(), "candles": rows})
    return result


def evaluate(t, history=None):
    scope = (research.stamp(t["start"]) + timedelta(minutes=15)).isoformat()
    return research.evaluate_sample(t, history or [], research.cost_profile(), scope)


def test_cost_floor_is_inclusive_not_double_charged():
    p = research.cost_profile({"feeRate": ".0001", "sellTaxRate": ".0018", "slippageRate": ".0005"})
    assert Decimal(p["effectiveRoundTrip"]) == Decimal(".003")
    assert Decimal(research.cost_profile({"slippageRate": ".002"})["effectiveRoundTrip"]) == Decimal(".004")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.1", ".06", True])
def test_invalid_costs_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        research.cost_profile({"feeRate": value})


def test_same_time_volume_excludes_current_future_and_missing(technical):
    history = historical(technical)
    result = research.time_relative_volume(technical, history)
    assert result["samples"] == 10 and result["ratio"] == 2
    assert research.time_relative_volume(technical, history[:9])["ratio"] is None
    future = deepcopy(history[0]); future["date"] = "2099-01-01"
    assert research.time_relative_volume(technical, [*history, future]) == result
    history[0]["candles"].pop(2)
    result = research.time_relative_volume(technical, history)
    assert result["samples"] == 9 and result["excluded"] == 1 and result["ratio"] is None


def test_large_price_discontinuity_is_not_valid_volume_baseline(technical):
    history = historical(technical)
    for row in history[0]["candles"]:
        for key in ("openPrice", "closePrice", "lowPrice", "highPrice"):
            row[key] = str(Decimal(row[key]) * 2)
    assert research.time_relative_volume(technical, history)["samples"] == 9


def test_retest_is_independent_from_breakout_and_does_not_mutate_input(technical):
    old = deepcopy(technical)
    signals, volume, _ = evaluate(technical, historical(technical))
    assert not signals["baseline"]["allowed"]
    assert signals["breakout_retest"]["allowed"], signals
    assert Decimal(signals["breakout_retest"]["stopPrice"]) == 10000
    assert volume["ratio"] == 2
    assert technical == old


def test_retest_cannot_reuse_breakout_before_candidate_was_observed(technical):
    signals, _, _ = research.evaluate_sample(technical, [], research.cost_profile(), technical["asOf"])
    assert not signals["breakout_retest"]["allowed"]


@pytest.mark.parametrize("key,value,reason", [("min_relative_volume", "10", "거래량"),
    ("min_session_turnover", "999999999999", "거래대금"), ("max_spread", ".00001", "스프레드"),
    ("position_stop_loss", ".001", "손실 한도")])
def test_retest_preserves_common_safety_rules(technical, key, value, reason):
    technical["settings"][key] = value
    signal = evaluate(technical)[0]["breakout_retest"]
    assert not signal["allowed"]
    assert any(reason in r for r in signal["reasons"])


def test_time_volume_substitutes_only_volume_test(technical):
    signals, _, _ = evaluate(technical, historical(technical))
    assert "직전 15분 고점 돌파 미확인" in signals["time_volume"]["reasons"]
    assert "동일 시간대 거래량 표본 부족" in evaluate(technical)[0]["time_volume"]["reasons"]


def test_market_drop_still_blocks_experiments(technical):
    for row in technical["markets"]["KOSDAQ"][-5:]:
        row.update(openPrice="9700", highPrice="9710", lowPrice="9690", closePrice="9700")
    for signal in evaluate(technical, historical(technical))[0].values():
        assert not signal["allowed"] and "시장 지수 하락 제한" in signal["reasons"]


def test_consumed_or_broken_retest_does_not_rearm(technical):
    start = research.stamp(technical["start"])
    for minute in range(26, 31):
        technical["candles"].append({**technical["candles"][-1], "timestamp": (start + timedelta(minutes=minute)).isoformat()})
    five, _, _ = bars(technical["candles"], start, start + timedelta(minutes=30))
    assert not research.breakout_retest(five, scope_start=start, tick=Decimal(10))["allowed"]
    five.loc[five.index[-2], "close"] = 10000
    assert not research.breakout_retest(five, scope_start=start, tick=Decimal(10))["allowed"]


def test_same_bar_future_prices_cannot_change_signal(technical):
    old = evaluate(technical)[0]
    technical["candles"].append({**technical["candles"][-1], "timestamp": "2026-09-15T09:26:00+09:00", "highPrice": "999999"})
    assert evaluate(technical)[0] == old


def test_report_preserves_raw_signal_scope_and_no_promotion(technical):
    sample = {"technical": technical, "scopeStart": technical["start"], "source": "five-minute-observation"}
    report = research.build_report([sample, sample], historical(technical), research.cost_profile(), now=research.stamp(technical["asOf"]))
    assert report["totals"]["breakout_retest"]["signals"] == 1
    assert report["totals"]["breakout_retest"]["outcomes"] == {"unavailable": 1}
    assert report["promotionAllowed"] is False
    assert not report["errors"]


def test_same_bar_is_not_counted_again_as_a_legacy_analysis(technical):
    sample = {"technical": technical, "scopeStart": technical["start"], "source": "five-minute-observation"}
    legacy = {**sample, "scopeStart": technical["asOf"], "source": "recorded-analysis"}
    report = research.build_report([legacy, sample], [], research.cost_profile(), now=research.stamp(technical["asOf"]))
    assert len(report["observations"]) == 1
    assert report["totals"]["breakout_retest"]["signals"] == 1
    assert report["sources"] == {"five-minute-observation": 1}


def test_missing_strategy_evidence_is_not_counted_as_a_hold(technical):
    sample = {"technical": technical, "scopeStart": technical["asOf"], "source": "recorded-analysis"}
    report = research.build_report([sample], [], research.cost_profile(), now=research.stamp(technical["asOf"]))
    assert report["totals"]["baseline"]["evaluated"] == 1
    for name in ("time_volume", "breakout_retest"):
        assert report["totals"][name]["evaluated"] == 0
        assert report["observations"][0]["results"][name]["allowed"] is None


def closing_prices(t):
    start = research.stamp(t["start"])
    rows = deepcopy(t["candles"])
    for minute in range(26, 391):
        rows.append({"timestamp": (start + timedelta(minutes=minute)).isoformat(), "currency": "KRW",
                     "openPrice": "10040", "highPrice": "10050", "lowPrice": "10030", "closePrice": "10040", "volume": "2000"})
    return {"symbol": "005930", "date": t["date"], "start": t["start"], "end": t["end"], "candles": rows}


def test_path_waits_for_future_interval_and_applies_cost_once(technical):
    signals, _, cfg = evaluate(technical)
    closing = closing_prices(technical)
    closing["candles"][25].update(highPrice="99999", lowPrice="1")
    closing["candles"][27].update(highPrice="10150", closePrice="10100")
    path = research.path_estimate(technical, signals["breakout_retest"], cfg, closing)
    assert path["entryAt"].endswith("09:26:00+09:00")
    assert path["outcome"] == "target"
    assert Decimal(path["estimatedNetReturn"]) == Decimal(".007")
    assert "not-a-fill" in path["scope"]


def test_path_with_both_barriers_never_awards_a_win(technical):
    signals, _, cfg = evaluate(technical)
    closing = closing_prices(technical)
    closing["candles"][26].update(highPrice="10150", lowPrice="9990")
    path = research.path_estimate(technical, signals["breakout_retest"], cfg, closing)
    assert path["outcome"] == "ambiguous" and path["estimatedNetReturn"] is None


def test_missing_exit_candles_and_shortened_session_fail_closed(technical):
    signals, _, cfg = evaluate(technical)
    closing = closing_prices(technical)
    changed = {**cfg, "market_close": "10:25"}
    assert research.path_estimate(technical, signals["breakout_retest"], changed, closing)["outcome"] == "entry-invalidated"
    closing["candles"].pop(100)
    with pytest.raises(ValueError):
        research.path_estimate(technical, signals["breakout_retest"], cfg, closing)


def test_outside_buy_time_and_bad_book_fail_closed(technical):
    technical["settings"]["buy_start_minutes"] = 60
    with pytest.raises(ValueError):
        evaluate(technical)
    technical["settings"]["buy_start_minutes"] = 20
    technical["orderbook"]["timestamp"] = technical["start"]
    with pytest.raises(ValueError):
        evaluate(technical)


def test_research_backend_cannot_place_or_cancel_orders():
    backend = SimpleNamespace(clock=lambda: None)
    reader = research.ReadOnlyMarket(backend)
    for method in ("place_order", "cancel_order", "get_holdings"):
        with pytest.raises(ValueError):
            reader.call(method)


@pytest.fixture
def service(tmp_path, monkeypatch, technical):
    monkeypatch.setattr(research, "ARCHIVE_ROOT", tmp_path / "external")
    monkeypatch.setattr(research, "archive_available", lambda: None)
    now = research.stamp(technical["asOf"])
    settings = {**technical["settings"], "strategy_observation": True}
    backend = SimpleNamespace(clock=lambda: now)
    s = research.StrategyComparisonService(tmp_path / "local", "dry-run", "paper", backend, lambda: settings, clock=lambda: now)
    return s, settings


def test_profile_versioning_is_research_only(service):
    s, _ = service
    first = s.save_profile({"revision": 0, "rates": {"slippageRate": ".002"}})
    assert first["profile"]["revision"] == 1
    assert not (s.local_dir / "accounts/paper.sqlite3").exists()
    with pytest.raises(ValueError):
        s.save_profile({"revision": 0, "rates": {}})
    assert (research.ARCHIVE_ROOT / "dry-run/paper/costs/000001.json").exists()


def test_offline_comparison_never_calls_network(service, technical, monkeypatch):
    s, _ = service
    monkeypatch.setattr(research, "load_sources", lambda *a: ([{"technical": technical, "scopeStart": technical["start"], "source": "five-minute-observation"}], historical(technical), [], []))
    s.submit(); s.thread.join(5)
    state = s.snapshot()
    assert not state["error"] and state["report"]["totals"]["breakout_retest"]["signals"] == 1
    assert state["report"]["promotionAllowed"] is False
    assert (research.ARCHIVE_ROOT / "dry-run/paper/comparisons" / state["report"]["id"] / "evidence.json").exists()


def test_watch_scope_dedup_settings_and_cancel(service, monkeypatch):
    s, settings = service
    calls = []
    monkeypatch.setattr(s, "_start", lambda *args: calls.append(args))
    s.register(["005930.KS", "000660.KS"], {"members": ["005930.KS"]}, settings)
    assert len(s.watches) == 1
    s.tick(s.clock()); s.tick(s.clock())
    assert len(calls) == 1
    settings["min_relative_volume"] = "2"
    s.tick(s.clock() + timedelta(minutes=5))
    assert not s.watches
    s.register(["005930.KS"], {"members": ["005930.KS"]}, settings)
    settings["strategy_observation"] = False
    s.tick(s.clock())
    assert not s.watches


def test_missing_external_storage_blocks_reads(service, monkeypatch):
    s, settings = service
    monkeypatch.setattr(research, "archive_available", lambda: (_ for _ in ()).throw(OSError()))
    s.register(["005930.KS"], {"members": ["005930.KS"]}, settings)
    s.tick(s.clock()); s.thread.join(5)
    assert "OSError" in s.error


@pytest.mark.parametrize("reason", ["disabled", "paused", "expired", "cancelled"])
def test_late_observation_is_discarded(service, technical, monkeypatch, reason):
    s, settings = service
    s.register(["005930.KS"], {"members": ["005930.KS"]}, settings)
    def collect(*args):
        if reason == "disabled":
            settings["strategy_observation"] = False
        elif reason == "paused":
            s.guard = lambda: False
        elif reason == "expired":
            s.clock = lambda: research.stamp(technical["asOf"]) + timedelta(minutes=61)
        else:
            s.cancel()
        return technical
    monkeypatch.setattr(research, "collect_technical", collect)
    s.tick(s.clock()); s.thread.join(5)
    assert not list(research.ARCHIVE_ROOT.rglob("*.json"))


def test_research_guard_failure_does_not_escape_scheduler(service):
    s, settings = service
    s.register(["005930.KS"], {"members": ["005930.KS"]}, settings)
    s.guard = lambda: (_ for _ in ()).throw(ValueError())
    s.tick(s.clock())
    assert "ValueError" in s.error


def test_daily_archive_checksum_is_verified(tmp_path, technical, monkeypatch):
    from tests.test_weekly_review import write_day
    root = tmp_path / "postmarket"
    monkeypatch.setattr(research.daily, "ARCHIVE_ROOT", root)
    day = research.stamp(technical["start"]).date()
    prices = closing_prices(technical)
    path = write_day(root, day, {"orders": {}, "positions": {}, "days": {}}, extra={
        "candidatePrices": {"005930": {"status": "complete", "candles": prices["candles"]}},
        "replays": [{"technical": technical}]})
    now = research.stamp(technical["end"]) + timedelta(minutes=10)
    observations, history, sources, issues = research.load_sources("dry-run", "paper", now)
    assert len(observations) == len(history) == len(sources) == 1 and not issues
    assert observations[0]["scopeStart"] == technical["asOf"]
    with (path / "inputs.json.gz").open("wb") as stream:
        stream.write(b"broken")
    observations, history, _, issues = research.load_sources("dry-run", "paper", now)
    assert not observations and not history and issues


def test_http_auth_csrf_and_no_trading_commands(server, service, monkeypatch):
    s, _ = service
    server.strategy = s
    submitted = Mock(return_value={"available": True})
    monkeypatch.setattr(s, "submit", submitted)
    assert request(server, "GET", "/api/strategy")[0] == 401
    cookie = login(server)
    assert request(server, "GET", "/api/strategy", cookie=cookie)[0] == 200
    before = server.control.snapshot()
    for action in ("run", "costs", "paper-costs"):
        assert request(server, "POST", "/api/strategy/" + action, {})[0] == 401
        assert request(server, "POST", "/api/strategy/" + action, {}, cookie=cookie, extra={"Origin": "https://evil.test"})[0] == 403
    assert request(server, "POST", "/api/strategy/run", {}, cookie=cookie)[0] == 202
    submitted.assert_called_once()
    assert before == server.control.snapshot()


def test_http_paper_cost_requires_confirmation_and_idle_paused_account(server, service):
    from tradingagents.brokers.account_service import paper_account
    s, _ = service
    server.strategy = s
    server.risk = PositionRiskManager(s.local_dir, backend=SimpleNamespace())
    account = paper_account(s.local_dir, "10000000")
    cookie = login(server)
    payload = {"paperRevision": 0, "rates": {"feeRate": ".001", "sellTaxRate": ".001", "slippageRate": ".001"}}
    url = "/api/strategy/paper-costs"
    assert request(server, "POST", url, payload, cookie=cookie)[0] == 400
    payload["confirmed"] = True
    assert request(server, "POST", url, payload, cookie=cookie)[0] == 400
    server.control.pause(True)
    server.control.active = {"id": "running"}
    assert request(server, "POST", url, payload, cookie=cookie)[0] == 400
    server.control.active = None
    assert request(server, "POST", url, payload, cookie=cookie)[0] == 202
    assert account.cost_profile()["revision"] == 1
    assert request(server, "POST", url, payload, cookie=cookie)[0] == 400


def test_cost_update_uses_same_lock_order_as_entry_watcher(tmp_path, monkeypatch):
    from tradingagents.monitoring.control import RunController
    risk, control = PositionRiskManager(tmp_path, backend=SimpleNamespace()), RunController()
    control.pause(True)
    original = risk.store.locked
    events = []
    @contextmanager
    def locked():
        with original():
            events.append("risk")
            yield
    class ControllerLock:
        def __enter__(self):
            events.append("controller")
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(risk.store, "locked", locked)
    control.lock = ControllerLock()
    risk.change_paper_costs(research.cost_profile(), 0, control=control)
    assert events[:2] == ["risk", "controller"]


def test_paper_cost_change_preserves_old_fills_and_cash(tmp_path, technical):
    account = PaperAccount(tmp_path / "paper.sqlite3")
    account.initialize("10000000")
    now = research.stamp(technical["asOf"])
    quote = {"symbol": "005930", "currency": "KRW", "lastPrice": "10000", "timestamp": now.isoformat()}
    buy = TossOrderPlan(action="BUY", symbol="005930", quantity="1", order_type="MARKET", rating="BUY", reason="test")
    old = account.fill(buy, quote, "buy", now)
    assert old["costProfile"]["revision"] == 0
    with pytest.raises(ValueError, match="보유"):
        account.change_costs({"feeRate": ".001", "sellTaxRate": ".001", "slippageRate": "0"}, 0)
    sell = TossOrderPlan(action="SELL", symbol="005930", quantity="1", order_type="MARKET", rating="SELL", reason="test")
    account.fill(sell, quote, "sell", now)
    cash = account.view()["cash"]
    account.change_costs({"feeRate": ".001", "sellTaxRate": ".001", "slippageRate": "0"}, 0)
    assert account.view()["cash"] == cash
    assert account.fill(buy, quote, "buy", now) == old
    new = account.fill(buy, quote, "buy2", now)
    assert new["fee"] == "10.00" and new["costProfile"]["revision"] == 1


def test_risk_cost_update_blocks_live_and_pending_orders(tmp_path):
    risk = PositionRiskManager(tmp_path, backend=SimpleNamespace())
    with risk.store.locked():
        state = risk.store.load()
        state["unconfirmed"] = True
        risk.store.save(state)
    with pytest.raises(ValueError, match="주문"):
        risk.change_paper_costs({}, 0)
    risk.live = True
    with pytest.raises(ValueError, match="실투자"):
        risk.change_paper_costs({}, 0)


def test_ui_and_settings_keep_observation_opt_in():
    assert main.make_parser().parse_args([]).strategy_observation is False
    assert main.validate_trading_settings({"strategy_observation": True}) == {"strategy_observation": True}
    with pytest.raises(ValueError):
        main.validate_trading_settings({"strategy_observation": "true"})
    html = main.render_monitor_html({})
    for value in ("strategy-panel", "strategy-run", "strategy-cost-form", "strategy-paper-apply", "/assets/strategy.js"):
        assert value in html
    assert "breakout_retest" not in main.ENTRY_STRATEGIES
