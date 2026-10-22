from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import asyncio

import pytest

import main
from tradingagents.brokers.intraday import bars, evaluate_entry, session_candles, size_entry, CandleDataError
from tradingagents.brokers.paper_account import KST
from tradingagents.brokers.toss_client import TossClient
from tradingagents.brokers.toss_executor import TossOrderPlan


@pytest.fixture
def data():
    start = datetime(2026, 9, 15, 9, tzinfo=KST)
    rows = []
    for minute in range(1, 21):
        last = minute > 15
        rows.append({"timestamp": (start + timedelta(minutes=minute)).isoformat(), "currency": "KRW",
                     "openPrice": "10040" if last else "10000", "highPrice": "10070" if last else "10010",
                     "lowPrice": "10040" if last else "9990", "closePrice": "10060" if last else "10000",
                     "volume": "5000" if last else "1000"})
    now = start + timedelta(minutes=20, seconds=5)
    book = {"currency": "KRW", "timestamp": now.isoformat(), "asks": [{"price": "10060", "volume": "100"}],
            "bids": [{"price": "10050", "volume": "100"}]}
    return rows, book, {"KOSPI": deepcopy(rows), "KOSDAQ": deepcopy(rows)}, start, now


def check(data, **settings):
    rows, book, markets, start, now = data
    return evaluate_entry("005930", rows, book, markets, start, now, {"min_session_turnover": "1000000", **settings})


def test_breakout_uses_completed_bars_vwap_volume_spread_and_markets(data):
    result = check(data)
    assert result["allowed"], result
    assert result["relativeVolume"] == 5
    assert result["vwapEstimate"] < 10060
    assert result["barEnd"].endswith("09:20:00+09:00")
    assert Decimal(result["stopPrice"]) < 10050


def test_forming_bar_and_future_spike_do_not_change_signal(data):
    baseline = check(data)
    data[0].append({**data[0][-1], "timestamp": (data[4] + timedelta(seconds=55)).isoformat(), "highPrice": "999999"})
    assert check(data) == baseline


@pytest.mark.parametrize("change", ["missing", "stale", "currency", "inverted", "no-timezone", "nan"])
def test_bad_data_fails_closed(data, change):
    rows, book, _, _, _ = data
    if change == "missing":
        rows.pop(3)
    elif change == "stale":
        book["timestamp"] = (data[4] - timedelta(seconds=31)).isoformat()
    elif change == "currency":
        rows[0]["currency"] = "USD"
    elif change == "inverted":
        rows[0]["lowPrice"] = "20000"
    elif change == "no-timezone":
        rows[0]["timestamp"] = "2026-09-15T09:01:00"
    else:
        rows[0]["volume"] = "NaN"
    with pytest.raises(ValueError):
        check(data)


@pytest.mark.parametrize("settings,reason", [
    ({"min_relative_volume": "6"}, "거래량"),
    ({"min_session_turnover": "10000000000"}, "거래대금"),
    ({"max_spread": "0.0001"}, "스프레드"),
    ({"min_reward_risk": "10"}, "손익비"),
    ({"entry_strategy": "pullback"}, "눌림목"),
])
def test_entry_conditions_are_not_llm_overrides(data, settings, reason):
    result = check(data, **settings)
    assert not result["allowed"]
    assert any(reason in r for r in result["reasons"])


def test_index_drop_blocks_otherwise_valid_entry(data):
    for row in data[2]["KOSDAQ"][-5:]:
        row.update(openPrice="9700", highPrice="9710", lowPrice="9690", closePrice="9700")
    result = check(data)
    assert not result["allowed"]
    assert "시장 지수 하락 제한" in result["reasons"]


def test_pullback_can_pass_with_vwap_reclaim_and_volume(data):
    for row in data[0][-5:]:
        row.update(openPrice="10020", lowPrice="9980")
    result = check(data, entry_strategy="pullback")
    assert result["allowed"], result


def test_chasing_price_is_rejected(data):
    data[1]["asks"][0]["price"] = "11000"
    data[1]["bids"][0]["price"] = "10990"
    assert "신호 가격 이탈 / 추격 매수 차단" in check(data)["reasons"]


def test_opening_range_uses_fixed_opening_window_and_new_cross(data):
    assert check(data, entry_strategy='opening_range')['allowed']
    assert not check(data, entry_strategy='opening_range', opening_range_minutes=30)['allowed']
    # A later continuation above the same opening range is not a fresh breakout.
    for row in data[0][10:15]:
        row.update(openPrice='10020', highPrice='10040', lowPrice='10010', closePrice='10030')
    result = check(data, entry_strategy='opening_range', opening_range_minutes=10)
    assert not result['allowed'] and '장 초반 고점의 신규 돌파 미확인' in result['reasons']


def test_vwap_reclaim_requires_previous_close_below_its_own_vwap(data):
    assert check(data, entry_strategy='vwap_reclaim')['allowed']
    for row in data[0][10:15]:
        row.update(openPrice='10020', highPrice='10040', lowPrice='10010', closePrice='10030')
    result = check(data, entry_strategy='vwap_reclaim')
    assert not result['allowed'] and 'VWAP 하단에서 상단으로의 재돌파 미확인' in result['reasons']


@pytest.mark.parametrize('strategy', ['opening_range', 'vwap_reclaim', 'breakout', 'pullback'])
def test_new_strategy_settings_and_controls(strategy):
    assert main.validate_trading_settings({'entry_strategy': strategy})['entry_strategy'] == strategy
    assert main.make_parser().parse_args(['--entry-strategy', strategy]).entry_strategy == strategy
    html = main.render_monitor_html({})
    assert 'value="' + strategy + '"' in html
    assert 'name="entry_watch_minutes"' in html and 'id="entry-watches"' in html
    with pytest.raises(ValueError):
        main.validate_trading_settings({'opening_range_minutes': 7})


def test_candles_use_end_timestamps_and_opening_boundary(data):
    rows, _, _, start, now = data
    rows.insert(0, {**rows[0], "timestamp": start.isoformat(), "closePrice": "50000"})
    five, _, _ = bars(rows, start, now)
    assert len(five) == 4
    assert five.iloc[0].close == 10000
    with pytest.raises(ValueError):
        bars(rows, start, start + timedelta(minutes=19))


def test_pagination_includes_opening_bar_and_deduplicates_overlap(data):
    rows, _, _, start, now = data
    calls = []
    def call(method, symbol, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return {"candles": list(reversed(rows[10:])), "nextBefore": rows[10]["timestamp"]}
        return {"candles": list(reversed(rows[:11])), "nextBefore": None}
    result = session_candles(call, "005930", start, now)
    assert len(result) == 20
    assert calls[1]["before"] == rows[10]["timestamp"]


def test_pagination_cannot_loop_forever(data):
    rows, _, _, start, now = data
    calls = []
    def call(*a, **kw):
        calls.append(kw)
        return {"candles": [rows[-1]], "nextBefore": rows[-1]["timestamp"]}
    with pytest.raises(CandleDataError) as error:
        session_candles(call, "005930", start, now)
    assert len(calls) == 3  # Two pagination calls plus one bounded gap repair.
    assert error.value.diagnostics['paginationStop'] == 'cursor-not-moving'


def test_market_indicator_start_times_are_normalized_to_end_times(data):
    rows, _, _, start, now = data
    raw = [{**r, "timestamp": (datetime.fromisoformat(r["timestamp"]) - timedelta(minutes=1)).isoformat()} for r in rows]
    result = session_candles(lambda *a, **kw: {"candles": raw, "nextBefore": None}, "KOSPI", start, now, index=True)
    assert result == rows


def test_sizing_respects_cost_budget_risk_and_explicit_quantity():
    signal = {"ask": "10000", "stopFraction": ".005"}
    settings = {"paper_capital": "10000000", "trade_risk_fraction": ".0001", "estimated_cost_rate": ".003"}
    plan = TossOrderPlan("BUY", "Buy", "005930", order_amount="1000000")
    assert size_entry(plan, signal, settings) == 12  # 1000 / (50 + 30), floored
    assert size_entry(TossOrderPlan("BUY", "Buy", "005930", quantity="2"), signal, settings) == 2
    assert size_entry(TossOrderPlan("BUY", "Buy", "005930", order_amount="100"), signal, settings) == 0


@pytest.mark.parametrize("key,value", [
    ("position_target_return", "NaN"), ("position_stop_loss", "-1"), ("daily_loss_limit", "Infinity"),
    ("trade_risk_fraction", "bad"), ("estimated_cost_rate", True), ("min_relative_volume", "0"),
    ("stagnation_minutes", "1.5"), ("max_consecutive_stops", "0"), ("entry_strategy", "random"),
])
def test_web_rejects_invalid_strategy_settings(key, value):
    with pytest.raises(ValueError):
        main.validate_trading_settings({key: value})


def test_settings_persist_and_legacy_daily_goal_is_not_used(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    args = main.make_parser().parse_args([])
    main.initialize_trading_settings(args)
    main.save_trading_settings({"position_target_return": ".015", "daily_target_return": ".9", "entry_strategy": "pullback"})
    main.apply_trading_settings(args)
    objective = main.trading_objective(args)
    assert objective["position_target_return"] == "0.015"
    assert "daily_target_return" not in objective
    page = main.render_monitor_html(main.build_monitor_snapshot())
    assert 'name="position_target_return"' in page
    assert 'name="daily_target_return"' not in page
    assert 'name="entry_strategy"' in page


def test_toss_chart_and_orderbook_read_contract(monkeypatch):
    calls = []
    async def get(self, path, **kw):
        calls.append((path, kw))
        return {}
    monkeypatch.setattr(TossClient, "get", get)
    async def run():
        async with TossClient("fake", "fake") as client:
            await client.get_candles("005930", before="2026-09-15T09:00:00+09:00")
            await client.get_candles("KOSPI", index=True)
            await client.get_orderbook("005930")
    asyncio.run(run())
    assert calls[0] == ("/api/v1/candles", {"params": {"interval": "1m", "count": 200, "before": "2026-09-15T09:00:00+09:00", "symbol": "005930", "adjusted": "false"}})
    assert calls[1][0] == "/api/v1/market-indicators/KOSPI/candles"
    assert calls[2][0] == "/api/v1/orderbook"
