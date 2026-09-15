from datetime import datetime, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.brokers.paper_account import PaperAccount, KST, period_start
from tradingagents.brokers.toss_executor import TossOrderPlan

NOW = datetime(2026, 9, 11, 10, 0, tzinfo=KST)


def quote(price="10", now=NOW):
    return {"symbol": "005930", "lastPrice": price, "timestamp": now.isoformat(), "currency": "KRW", "source": "test"}


def plan(side="BUY", quantity="50", amount=None):
    return TossOrderPlan(side, "Buy" if side == "BUY" else "Sell", "005930", quantity=quantity, order_amount=amount)


@pytest.fixture
def account(tmp_path):
    account = PaperAccount(tmp_path / "paper.sqlite3")
    account.initialize("1000", fee_rate="0.01", tax_rate="0.02", now=NOW)
    return account


def test_cash_holdings_cost_and_realized_profit(account):
    buy = account.fill(plan(), quote(), "buy", now=NOW)
    assert buy["status"] == "SIMULATED_FILLED"
    assert buy["cashAfter"] == "495.00"
    assert buy["fee"] == "5.00"
    sell = account.fill(plan("SELL", "20"), quote("12"), "sell", now=NOW)
    assert sell["realizedPnl"] == "30.80"
    summary = account.view(NOW)
    assert summary["cash"] == "727.80"
    assert summary["equity"] == "1087.80"
    assert summary["positions"][0]["quantity"] == "30"
    assert summary["positions"][0]["cost"] == "303.00"
    assert summary["unrealizedPnl"] == "57.00"
    assert Decimal(account.returns("day", "2026-09-11")[0]["returnPct"]) == Decimal("8.78")


def test_idempotence_and_capital_change_never_reset_account(account):
    original = account.fill(plan(), quote(), "same", now=NOW)
    assert account.fill(plan(), quote("20"), "same", now=NOW) == original
    account.initialize("99999", now=NOW)
    restored = PaperAccount(account.path).view(NOW)
    assert restored["cash"] == "495.00" and len(restored["trades"]) == 1
    assert restored["initialCapital"] == "1000.00"


@pytest.mark.parametrize("order", [plan("SELL", "1"), plan("BUY", "1000"), plan("BUY", "0"), plan("BUY", "0.1")])
def test_rejections_leave_ledger_unchanged(account, order):
    rejected = account.fill(order, quote(), "rejected", now=NOW)
    assert rejected["status"] == "SIMULATED_REJECTED"
    assert account.view(NOW)["cash"] == "1000.00"
    assert account.view(NOW)["positions"] == []


@pytest.mark.parametrize("bad", [None, quote("NaN"), quote("0"), quote("10", NOW-timedelta(hours=1)),
    quote("10", NOW+timedelta(hours=1)), {**quote(), "currency": "USD"}, {**quote(), "symbol": "000660"}])
def test_no_fill_without_fresh_matching_quote(account, bad):
    assert account.fill(plan(), bad, "bad", now=NOW)["status"] == "SIMULATED_REJECTED"
    assert account.view(NOW)["cash"] == "1000.00"


def test_budget_includes_fees_and_uses_whole_shares(account):
    fill = account.fill(plan(quantity=None, amount="1000"), quote(), "budget", now=NOW)
    assert fill["quantity"] == "99"
    assert fill["cashAfter"] == "0.10"


def test_concurrent_duplicate_execution_only_fills_once(account):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: account.fill(plan(), quote(), "same", now=NOW), range(4)))
    assert results[0] == results[-1]
    assert len(account.view(NOW)["trades"]) == 1


def test_missing_quote_never_values_stock_at_zero(account):
    account.fill(plan(), quote(), "buy", now=NOW)
    stale = account.refresh({}, now=NOW+timedelta(hours=1))
    assert stale["equity"] is None
    assert stale["positions"][0]["marketValue"] is None
    assert account.returns("day", "2026-09-11")[0]["endAt"] == NOW.isoformat()


def test_day_week_month_year_and_no_future_leakage(account):
    account.fill(plan(), quote(), "buy", now=NOW)
    monday = datetime(2026, 9, 14, 15, 20, tzinfo=KST)
    october = datetime(2026, 10, 1, 15, 20, tzinfo=KST)
    new_year = datetime(2027, 1, 4, 15, 20, tzinfo=KST)
    for now, price in [(monday, "12"), (october, "14"), (new_year, "16")]:
        account.refresh({"005930": quote(price, now)}, now=now)
    daily = account.returns("day", "2026-09-14")
    assert daily[0]["period"] == "2026-09-14"
    assert daily[0]["startEquity"] == "995.00"
    assert daily[0]["endEquity"] == "1095.00"
    assert account.returns("week", "2026-09-14")[0]["period"] == "2026-09-14"
    assert account.returns("month", "2026-10-01")[0]["period"] == "2026-10-01"
    assert account.returns("year", "2027-01-04")[0]["period"] == "2027-01-01"
    assert account.returns("day", "2020-01-01") == []
    assert period_start(new_year.date(), "week").isoformat() == "2027-01-04"


def test_sell_all_clears_cost(account):
    account.fill(plan(), quote(), "buy", now=NOW)
    account.fill(plan("SELL", "50"), quote("12"), "sell", now=NOW)
    assert account.view(NOW)["positions"] == []
    assert account.view(NOW)["realizedPnl"] == "77.00"
