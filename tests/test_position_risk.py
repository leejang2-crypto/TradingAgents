from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.brokers.paper_account import KST, number
from tradingagents.brokers.toss_executor import TossOrderPlan


class FakeBroker:
    def __init__(self, clock):
        self.clock, self.positions, self.orders = clock, {}, {}
        self.price, self.auto_fill, self.fail_submit = Decimal(100), True, False
        self.cancels, self.holidays = [], set()
        self.fail_prices = False

    def calendar(self, day):
        day = date.fromisoformat(day)
        def business(d):
            return d.weekday() < 5 and d not in self.holidays
        previous, following = day - timedelta(days=1), day + timedelta(days=1)
        while not business(previous):
            previous -= timedelta(days=1)
        while not business(following):
            following += timedelta(days=1)
        return {"today": {"date": day.isoformat(), "integrated": {"regularMarket": {
            "startTime": day.isoformat() + "T09:00:00+09:00", "endTime": day.isoformat() + "T15:30:00+09:00"}} if business(day) else None},
            "previousBusinessDay": {"date": previous.isoformat()}, "nextBusinessDay": {"date": following.isoformat()}}

    def holdings(self):
        return dict(self.positions)

    def prices(self, symbols):
        if self.fail_prices:
            raise TimeoutError("no quotes")
        return {s: {"symbol": s, "currency": "KRW", "lastPrice": str(self.price), "timestamp": self.clock().isoformat()} for s in symbols}

    def entry_signal(self, symbol, start, now, settings):
        return {"allowed": True, "reasons": [], "symbol": symbol, "at": now.isoformat(), "ask": str(self.price),
                "stopPrice": str(self.price * Decimal('.995')), "stopFraction": ".005"}

    def purchase_basis(self, symbol, quantity):
        return Decimal(100)

    def open_orders(self):
        return [deepcopy(o) for o in self.orders.values() if o["status"] not in {"FILLED", "CANCELED", "REJECTED"}]

    def sellable(self, symbol):
        return self.positions.get(symbol, Decimal(0))

    def submit(self, payload, now, *, valid_until=None):
        key = "broker-" + str(len(self.orders) + 1)
        self.orders[key] = {"orderId": key, "symbol": payload["symbol"], "side": payload["side"], "currency": "KRW",
                            "status": "PENDING", "quantity": payload.get("quantity"), "orderedAt": now.isoformat(), "payload": payload,
                            "execution": {"filledQuantity": "0", "filledAmount": None, "commission": None, "tax": None, "filledAt": None}}
        if self.auto_fill:
            quantity = number(payload["quantity"]) if payload.get("quantity") else (number(payload["orderAmount"]) / self.price).to_integral_value()
            self.fill(key, quantity, "FILLED")
        if self.fail_submit:
            raise TimeoutError("response lost after submission")
        return {"orderId": key}

    def fill(self, key, quantity, status):
        order = self.orders[key]
        delta = number(quantity) - number(order["execution"]["filledQuantity"])
        s = order["symbol"]
        self.positions[s] = self.positions.get(s, Decimal(0)) + (delta if order["side"] == "BUY" else -delta)
        order.update(status=status, execution={"filledQuantity": str(quantity), "filledAmount": str(number(quantity) * self.price),
                                               "commission": "0", "tax": "0", "filledAt": self.clock().isoformat()})

    def detail(self, order):
        return deepcopy(self.orders[order["orderId"]])

    def cancel(self, order_id):
        self.cancels.append(order_id)
        return {"orderId": order_id}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "fake-account")
    time = [datetime(2026, 9, 15, 10, tzinfo=KST)]
    clock = lambda: time[0]
    broker = FakeBroker(clock)
    settings = {"paper_capital": "10000", "position_target_return": ".01", "estimated_cost_rate": "0", "stagnation_minutes": 360}
    manager = PositionRiskManager(tmp_path, live=True, backend=broker, settings=lambda: settings, clock=clock)
    return manager, broker, time, settings


def buy(manager, symbol="005930"):
    return manager.execute(TossOrderPlan("BUY", "Buy", symbol, order_amount="1000"))


def test_stock_stop_loss_exits_without_llm(env):
    manager, broker, _, _ = env
    buy(manager)
    broker.price = Decimal("99.5")
    manager.tick()
    assert manager.snapshot()["positions"]["005930"]["exitReason"] == "stop-loss"
    assert broker.orders["broker-2"]["payload"]["quantity"] == "10"


def test_only_winning_position_is_liquidated(env):
    manager, broker, time, _ = env
    buy(manager)
    buy(manager, "000660")
    broker.prices = lambda symbols: {s: {"symbol": s, "currency": "KRW", "lastPrice": "101" if s == "005930" else "100", "timestamp": time[0].isoformat()} for s in symbols}
    manager.tick()
    assert broker.orders["broker-3"]["symbol"] == "005930"
    assert not manager.snapshot()["positions"]["000660"]["liquidate"]


def test_loss_limit_latches_and_liquidates_all_positions(env):
    manager, broker, _, _ = env
    buy(manager)
    buy(manager, "000660")
    broker.price = Decimal("80")
    manager.tick()
    assert manager.snapshot()["daily"]["lossLimitReached"]
    assert broker.orders["broker-3"]["side"] == broker.orders["broker-4"]["side"] == "SELL"
    manager.tick()
    broker.price = Decimal(100)
    with pytest.raises(ValueError, match="손실 제한"):
        buy(manager, "000880")


def test_time_exit_only_for_stagnating_position(env):
    manager, broker, time, settings = env
    settings["stagnation_minutes"] = 90
    buy(manager)
    time[0] += timedelta(minutes=91)
    broker.price = Decimal("100.3")
    manager.tick()
    assert len(broker.orders) == 1
    broker.price = Decimal("100.1")
    manager.tick()
    assert manager.snapshot()["positions"]["005930"]["exitReason"] == "time-exit"


def test_consecutive_stops_block_entry_for_day(env):
    manager, broker, _, _ = env
    for symbol in ["005930", "000660", "000880"]:
        broker.price = Decimal(100)
        buy(manager, symbol)
        broker.price = Decimal("99.5")
        manager.tick()
        manager.tick()
    assert manager.snapshot()["daily"]["stopLimitReached"]
    assert manager.snapshot()["daily"]["consecutiveStops"] == 3
    with pytest.raises(ValueError, match="손실 제한"):
        buy(manager, "035420")


def test_target_stop_and_basis_survive_restart(env):
    manager, broker, time, settings = env
    buy(manager)
    restarted = PositionRiskManager(manager.local_dir, live=True, backend=broker, settings=lambda: settings, clock=lambda: time[0])
    broker.price = Decimal(101)
    restarted.tick()
    assert restarted.snapshot()["positions"]["005930"]["averagePrice"] == "100"
    assert restarted.snapshot()["positions"]["005930"]["exitReason"] == "position-target"


def test_failed_or_stale_entry_evidence_blocks_without_order(env):
    manager, broker, time, _ = env
    broker.entry_signal = lambda *a: {"allowed": True, "symbol": "005930", "at": (time[0] - timedelta(seconds=31)).isoformat()}
    result = buy(manager)
    assert result["skipped"] == "intraday-filter"
    assert not broker.orders
    broker.entry_signal = lambda *a: (_ for _ in ()).throw(TimeoutError("no candles"))
    assert buy(manager)["skipped"] == "intraday-filter"
    assert manager.snapshot()["entryChecks"]["005930"]["reasons"] == ["no candles"]


def test_candle_diagnostics_are_saved_without_sending_orders(env):
    from tradingagents.brokers.intraday import CandleDataError, candle_coverage
    manager, broker, clock, _ = env
    details = candle_coverage([], clock[0].replace(hour=9, minute=0), clock[0])
    details['symbol'] = 'KOSPI'
    def missing(*args):
        raise CandleDataError(details)
    broker.entry_signal = missing
    result = buy(manager)
    assert result['skipped'] == 'intraday-filter' and not broker.orders
    assert result['entryCheck']['dataDiagnostics']['symbol'] == 'KOSPI'
    assert manager.snapshot()['entryChecks']['005930']['dataDiagnostics']['missingCount'] > 0


@pytest.mark.parametrize('allowed', [False, True])
def test_index_opening_warning_survives_final_entry_decision(env, allowed):
    manager, broker, _, _ = env
    original = broker.entry_signal
    references = {'KOSPI': {'basisStart': '2026-09-15T09:01:00+09:00', 'openingGap': True}}
    warnings = ['KOSPI: first minute unavailable; actual observed open used']
    def signal(*args):
        return {**original(*args), 'allowed': allowed, 'reasons': [] if allowed else ['market drop'],
                'marketReturns': {'KOSPI': -.02 if not allowed else 0},
                'marketReferences': references, 'warnings': warnings}
    broker.entry_signal = signal
    buy(manager)
    saved = manager.snapshot()['entryChecks']['005930']
    assert saved['allowed'] is allowed
    assert saved['marketReferences'] == references and saved['warnings'] == warnings
    assert bool(broker.orders) is allowed


def test_no_pyramiding_or_same_day_reentry(env):
    manager, _, _, _ = env
    buy(manager)
    with pytest.raises(ValueError, match="추가 매수"):
        buy(manager)
    manager.execute(TossOrderPlan("SELL", "Sell", "005930", quantity="1"))
    with pytest.raises(ValueError, match="재진입"):
        buy(manager)


def test_buy_is_quantity_limit_order_with_risk_sizing(env):
    manager, _, _, settings = env
    settings.update(estimated_cost_rate=".003", trade_risk_fraction=".0001")
    result = buy(manager)
    assert result["payload"]["quantity"] == "1"
    assert result["payload"]["price"] == "100"
    assert result["payload"]["orderType"] == "LIMIT"
    assert "orderAmount" not in result["payload"]


def test_partial_entry_target_waits_for_buy_cancel_before_exit(env):
    manager, broker, _, _ = env
    broker.auto_fill = False
    buy(manager)
    broker.fill("broker-1", 3, "PARTIAL_FILLED")
    broker.price = Decimal(101)
    manager.tick()
    assert broker.cancels == ["broker-1"]
    assert len(broker.orders) == 1
    assert manager.snapshot()["positions"]["005930"]["exitReason"] == "position-target"
    broker.orders["broker-1"]["status"] = "CANCELED"
    manager.tick()
    assert broker.orders["broker-2"]["payload"]["quantity"] == "3"


def test_gross_target_excludes_entry_fees_and_reports_cost_basis(env):
    manager, broker, _, _ = env
    broker.auto_fill = False
    buy(manager)
    broker.fill("broker-1", 10, "FILLED")
    broker.orders["broker-1"]["execution"]["commission"] = "2"
    broker.price = Decimal(101)
    manager.tick()
    p = manager.snapshot()["positions"]["005930"]
    assert p["averagePrice"] == "100"
    assert p["costPerShare"] == "100.2"
    assert p["exitReason"] == "position-target"
    assert number(p["estimatedNetReturnPct"]) < 1


def test_entry_deadline_is_signal_expiry_even_when_session_is_open(env):
    manager, broker, time, _ = env
    original = broker.submit
    def submit(payload, now, *, valid_until):
        assert valid_until == time[0] + timedelta(seconds=30)
        return original(payload, now, valid_until=valid_until)
    broker.submit = submit
    buy(manager)


def test_signal_settings_change_during_fetch_cannot_submit(env):
    manager, broker, _, settings = env
    original = broker.entry_signal
    def changed(*args):
        signal = original(*args)
        settings["position_stop_loss"] = ".004"
        return signal
    broker.entry_signal = changed
    result = buy(manager)
    assert result["skipped"] == "intraday-filter"
    assert not broker.orders


def test_existing_positions_and_pending_orders_reserve_capital(env):
    manager, broker, _, settings = env
    settings["position_pct"] = "1"
    settings["trade_risk_fraction"] = ".02"
    broker.auto_fill = False
    first = manager.execute(TossOrderPlan("BUY", "Buy", "005930", order_amount="10000"))
    assert first["payload"]["quantity"] == "100"
    result = buy(manager, "000660")
    assert result["skipped"] == "intraday-filter"
    assert len(broker.orders) == 1


def test_default_same_day_liquidation_and_confirmed_full_exit(env):
    manager, broker, time, _ = env
    buy(manager)
    assert manager.snapshot()["positions"]["005930"]["quantity"] == "10"
    time[0] = time[0].replace(hour=15, minute=14)
    manager.tick()
    assert len(broker.orders) == 1
    time[0] = time[0].replace(minute=15)
    manager.tick()
    assert broker.orders["broker-2"]["payload"]["quantity"] == "10"
    assert manager.snapshot()["orders"][broker.orders["broker-2"]["payload"]["clientOrderId"]]["status"] == "PENDING"
    manager.tick()
    state = manager.snapshot()
    assert state["positions"]["005930"]["quantity"] == "0"
    assert state["positions"]["005930"]["closedAt"]
    assert len(broker.orders) == 2


def test_agent_sell_uses_all_managed_shares_not_default_one(env):
    manager, broker, _, _ = env
    buy(manager)
    result = manager.execute(TossOrderPlan("SELL", "Sell", "005930", quantity="1"))
    assert result["payload"]["quantity"] == "10"
    assert result["fillVerified"]
    assert broker.positions["005930"] == 0


def test_carry_is_explicit_and_expires_on_second_trading_day(env):
    manager, broker, time, _ = env
    time[0] = datetime(2026, 9, 18, 10, tzinfo=KST)  # Friday
    broker.holidays.add(date(2026, 9, 21))  # Monday holiday
    buy(manager)
    p = manager.snapshot()["positions"]["005930"]
    manager.carry("005930", p["cycle"], True)
    assert manager.snapshot()["positions"]["005930"]["maxHoldingDate"] == "2026-09-22"
    time[0] = time[0].replace(hour=15, minute=20)
    manager.tick()
    assert len(broker.orders) == 1
    time[0] = datetime(2026, 9, 22, 10, tzinfo=KST)
    with pytest.raises(ValueError):
        manager.carry("005930", p["cycle"], True)
    with pytest.raises(ValueError, match="추가 매수"):
        buy(manager)
    time[0] = time[0].replace(hour=15, minute=15)
    manager.tick()
    assert broker.orders["broker-2"]["side"] == "SELL"


def test_missed_liquidation_is_due_at_next_open(env):
    manager, broker, time, _ = env
    buy(manager)
    time[0] += timedelta(days=1)
    manager.tick()
    assert len(broker.orders) == 2


def test_target_is_per_position_and_does_not_lock_other_symbols(env):
    manager, broker, _, _ = env
    buy(manager)
    broker.price = Decimal(110)  # 10 shares * 10 profit = 1% of 10,000
    manager.tick()
    assert not manager.snapshot()["daily"]["targetReached"]
    assert manager.snapshot()["positions"]["005930"]["exitReason"] == "position-target"
    manager.tick()
    broker.price = Decimal(100)
    buy(manager, "000880")
    assert len(broker.orders) == 3


def test_partial_exit_waits_for_cancel_confirmation_then_only_sells_remainder(env):
    manager, broker, time, _ = env
    buy(manager)
    broker.auto_fill = False
    time[0] = time[0].replace(hour=15, minute=15)
    manager.tick()
    broker.fill("broker-2", 4, "PARTIAL_FILLED")
    manager.tick()
    assert manager.snapshot()["positions"]["005930"]["quantity"] == "6"
    assert len(broker.orders) == 2
    time[0] += timedelta(seconds=100)
    manager.tick()
    manager.tick()
    assert broker.cancels == ["broker-2"]
    assert len(broker.orders) == 2
    broker.fill("broker-2", 4, "CANCELED")
    manager.tick()
    assert broker.orders["broker-3"]["payload"]["quantity"] == "6"


def test_restart_reconciles_pending_order_without_duplicate_submission(env):
    manager, broker, time, settings = env
    broker.auto_fill = False
    buy(manager)
    broker.fill("broker-1", 10, "FILLED")
    restarted = PositionRiskManager(manager.local_dir, live=True, backend=broker, settings=lambda: settings, clock=lambda: time[0])
    restarted.tick()
    restarted.tick()
    assert restarted.snapshot()["positions"]["005930"]["quantity"] == "10"
    assert len(broker.orders) == 1


def test_lost_submit_response_never_replays_or_creates_new_orders(env):
    manager, broker, time, _ = env
    broker.fail_submit = True
    with pytest.raises(ValueError, match="불확실"):
        buy(manager)
    time[0] = time[0].replace(hour=15, minute=15)
    manager.tick()
    assert len(broker.orders) == 1
    assert manager.snapshot()["unconfirmed"]
    with pytest.raises(ValueError, match="기존 주문"):
        buy(manager, "000880")


def test_unrelated_live_holdings_are_not_adopted_or_sold(env):
    manager, broker, time, _ = env
    broker.positions["000880"] = Decimal(23)
    time[0] = time[0].replace(hour=15, minute=20)
    manager.tick()
    assert manager.snapshot()["unmanaged"] == {"000880": "23"}
    assert not broker.orders


def test_unrelated_same_symbol_shares_are_not_sold(env):
    manager, broker, _, _ = env
    buy(manager)
    # Shares acquired outside the strategy after entry must remain untouched.
    broker.positions["005930"] += Decimal(23)
    manager.execute(TossOrderPlan("SELL", "Sell", "005930", quantity="1"))
    assert broker.positions["005930"] == 23


def test_cutoff_and_stale_quotes_block_buys_but_not_deadline_exit(env):
    manager, broker, time, _ = env
    buy(manager)
    time[0] = time[0].replace(hour=15)
    with pytest.raises(ValueError, match="매수 제한"):
        buy(manager, "000880")
    broker.fail_prices = True
    time[0] = time[0].replace(minute=15)
    manager.tick()
    assert broker.orders["broker-2"]["side"] == "SELL"
    assert manager.snapshot()["valuationError"]


def test_invalid_fill_evidence_does_not_mutate_ledger(env):
    manager, broker, _, _ = env
    broker.auto_fill = False
    buy(manager)
    broker.fill("broker-1", 10, "FILLED")
    broker.orders["broker-1"]["currency"] = "USD"
    manager.tick()
    assert manager.snapshot()["cashDelta"] == "0"
    assert not manager.snapshot()["positions"]
    assert manager.snapshot()["error"]


def test_confirmed_quantity_is_managed_even_while_fees_are_missing(env):
    manager, broker, time, _ = env
    broker.auto_fill = False
    buy(manager)
    broker.fill("broker-1", 10, "FILLED")
    broker.orders["broker-1"]["execution"]["commission"] = None
    manager.tick()
    assert manager.snapshot()["positions"]["005930"]["quantity"] == "10"
    assert manager.snapshot()["valuationError"]
    time[0] = time[0].replace(hour=15, minute=15)
    manager.tick()
    assert broker.orders["broker-2"]["side"] == "SELL"
    broker.orders["broker-1"]["execution"]["commission"] = "2"
    manager.tick()
    assert manager.snapshot()["cashDelta"] == "-1002"


def test_stale_cycle_and_late_carry_approval_are_rejected(env):
    manager, _, time, _ = env
    buy(manager)
    p = manager.snapshot()["positions"]["005930"]
    with pytest.raises(ValueError):
        manager.carry("005930", "old-cycle", True)
    time[0] = time[0].replace(hour=15, minute=15)
    with pytest.raises(ValueError):
        manager.carry("005930", p["cycle"], True)


def test_calendar_failure_blocks_new_orders(env):
    manager, broker, _, _ = env
    broker.calendar = lambda *a: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        buy(manager)
    assert not broker.orders


def test_one_percent_stock_gain_exits_despite_smaller_account_return(env):
    manager, broker, _, _ = env
    buy(manager)
    broker.price = Decimal(101)
    manager.tick()
    assert not manager.snapshot()["daily"]["targetReached"]
    assert number(manager.snapshot()["daily"]["returnPct"]) == Decimal("0.1")
    assert broker.orders["broker-2"]["side"] == "SELL"


def test_closed_position_can_be_bought_in_new_cycle_next_day(env):
    manager, broker, time, _ = env
    buy(manager)
    old = manager.snapshot()["positions"]["005930"]["cycle"]
    manager.execute(TossOrderPlan("SELL", "Sell", "005930", quantity="1"))
    time[0] += timedelta(days=1)
    buy(manager)
    assert manager.snapshot()["positions"]["005930"]["cycle"] != old
    assert manager.snapshot()["positions"]["005930"]["acquiredDate"] == "2026-09-16"


def test_external_sell_mismatch_blocks_profit_trigger_and_new_buys(env):
    manager, broker, _, _ = env
    buy(manager)
    broker.positions["005930"] = Decimal(3)
    broker.price = Decimal(200)
    manager.tick()
    assert manager.snapshot()["valuationError"]
    assert not manager.snapshot()["daily"]["targetReached"]
    with pytest.raises(ValueError):
        buy(manager, "000880")


def test_pending_buy_is_cancelled_at_cutoff_without_claiming_cancel_complete(env):
    manager, broker, time, _ = env
    broker.auto_fill = False
    buy(manager)
    time[0] = time[0].replace(hour=15)
    manager.tick()
    assert broker.cancels == ["broker-1"]
    assert next(iter(manager.snapshot()["orders"].values()))["status"] == "PENDING"
    manager.tick()
    assert len(broker.cancels) == 1


def test_rejected_exits_have_a_bounded_retry_count(env):
    manager, broker, time, _ = env
    buy(manager)
    broker.auto_fill = False
    time[0] = time[0].replace(hour=15, minute=15)
    for n in range(2, 5):
        manager.tick()
        broker.fill("broker-" + str(n), 0, "REJECTED")
        time[0] += timedelta(seconds=30)
    manager.tick()
    manager.tick()
    assert len(broker.orders) == 4
    assert "3회" in manager.snapshot()["positions"]["005930"]["alert"]


def test_explicit_adoption_tracks_date_without_counting_import_value_as_profit(env):
    manager, broker, _, _ = env
    broker.positions["005930"] = Decimal(7)
    manager.tick()
    assert manager.snapshot()["unmanaged"]["005930"] == "7"
    with pytest.raises(ValueError):
        manager.adopt("005930", "2026-09-14", False)
    manager.adopt("005930", "2026-09-14", True)
    state = manager.snapshot()
    assert state["positions"]["005930"]["quantity"] == "7"
    assert state["positions"]["005930"]["acquiredDate"] == "2026-09-14"
    assert "005930" not in state["unmanaged"]
    assert state["cashDelta"] == "-700"
    manager.tick()
    assert broker.orders["broker-1"]["side"] == "SELL"
    assert number(manager.snapshot()["daily"]["pnl"]) == 0


def test_adoption_rejects_future_or_holiday_dates(env):
    manager, broker, _, _ = env
    broker.positions["005930"] = Decimal(1)
    for day in ("2026-09-16", "2026-09-13"):
        with pytest.raises(ValueError):
            manager.adopt("005930", day, True)


def test_real_paper_ledger_is_used_for_both_entry_and_full_exit(env):
    from tradingagents.brokers.position_risk import BrokerBackend
    from tradingagents.brokers.account_service import paper_account
    manager, fake, time, settings = env
    backend = BrokerBackend(manager.local_dir, False, "10000", clock=lambda: time[0])
    backend.prices = fake.prices
    backend.calendar = fake.calendar
    backend.entry_signal = fake.entry_signal
    paper = PositionRiskManager(manager.local_dir, backend=backend, settings=lambda: settings, clock=lambda: time[0])
    result = buy(paper)
    assert result["paperExecution"]["status"] == "SIMULATED_FILLED"
    assert not result["fillVerified"]
    assert paper_account(manager.local_dir).symbols() == ["005930"]
    time[0] = time[0].replace(hour=15, minute=15)
    paper.tick()
    paper.tick()
    assert paper_account(manager.local_dir).symbols() == []
    assert paper.snapshot()["positions"]["005930"]["quantity"] == "0"


def test_paper_history_reconstructs_original_acquisition_day(env):
    from tradingagents.brokers.position_risk import BrokerBackend
    from tradingagents.brokers.account_service import paper_account
    manager, fake, time, settings = env
    account = paper_account(manager.local_dir, "10000")
    account.fill(TossOrderPlan("BUY", "Buy", "005930", quantity="3"), fake.prices(["005930"])["005930"], "legacy", now=time[0])
    time[0] += timedelta(days=1)
    backend = BrokerBackend(manager.local_dir, False, "10000", clock=lambda: time[0])
    backend.prices, backend.calendar = fake.prices, fake.calendar
    paper = PositionRiskManager(manager.local_dir, backend=backend, settings=lambda: settings, clock=lambda: time[0])
    paper.tick()
    assert paper.snapshot()["positions"]["005930"]["acquiredDate"] == "2026-09-15"
    paper.tick()
    assert account.symbols() == []


def test_deadline_guard_runs_after_token_acquisition_without_sending_http():
    import asyncio
    from unittest.mock import AsyncMock
    from tradingagents.brokers.toss_client import TossClient, OrderWindowClosed
    async def run():
        async with TossClient("fake", "fake", "fake") as client:
            client._headers = AsyncMock(return_value={})
            client._client.post = AsyncMock()
            client.order_valid_until = datetime(2000, 1, 1, tzinfo=KST)
            with pytest.raises(OrderWindowClosed):
                await client.create_order({"symbol": "005930"})
            client._client.post.assert_not_called()
    asyncio.run(run())
