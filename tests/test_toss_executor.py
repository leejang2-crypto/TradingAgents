from __future__ import annotations

from decimal import Decimal

from tradingagents.brokers.toss_executor import (
    order_payload,
    plan_from_decision,
    record_paper_trade,
    toss_symbol,
)


def test_toss_symbol_removes_yahoo_korea_suffix():
    assert toss_symbol("005930.KS") == "005930"
    assert toss_symbol("035720.KQ") == "035720"
    assert toss_symbol("AAPL") == "AAPL"


def test_buy_plan_from_overweight_decision_uses_budget_and_price():
    plan = plan_from_decision(
        "**Rating**: Overweight\n",
        "005930.KS",
        price=Decimal("70000"),
        max_order_amount=Decimal("150000"),
    )
    assert plan.action == "BUY"
    assert plan.symbol == "005930"
    assert plan.quantity == "2"
    payload = order_payload(plan)
    assert payload["side"] == "BUY"
    assert payload["symbol"] == "005930"
    assert payload["quantity"] == "2"


def test_hold_plan_has_no_order_payload():
    plan = plan_from_decision("**Rating**: Hold", "005930.KS")
    assert plan.action == "HOLD"
    assert order_payload(plan) == {}


def test_record_paper_trade_writes_jsonl(tmp_path):
    plan = plan_from_decision(
        "**Rating**: Overweight\n",
        "005930.KS",
        price=Decimal("70000"),
        max_order_amount=Decimal("150000"),
    )
    payload = order_payload(plan)
    result = record_paper_trade(plan, payload, log_path=tmp_path / "trades.jsonl")
    assert result["record"]["mode"] == "dry-run"
    assert result["record"]["status"] == "FILLED"
    assert result["record"]["payload"]["symbol"] == "005930"
    assert (tmp_path / "trades.jsonl").read_text(encoding="utf-8").strip()
