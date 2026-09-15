from __future__ import annotations

import os
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.monitoring.control import RunStopped, checkpoint

from .toss_client import TossClient


BUY_RATINGS = {"Buy", "Overweight"}
SELL_RATINGS = {"Sell", "Underweight"}


@dataclass(frozen=True)
class TossOrderPlan:
    action: str
    rating: str
    symbol: str
    quantity: str | None = None
    order_amount: str | None = None
    order_type: str = "MARKET"
    time_in_force: str = "DAY"
    reason: str = ""


def toss_symbol(symbol: str) -> str:
    """Convert analysis symbols like 005930.KS / 005930.KQ to Toss symbols."""
    return symbol.strip().upper().split(".", 1)[0]


def _client_order_id(side: str, symbol: str) -> str:
    clean = "".join(ch for ch in symbol if ch.isalnum())[:8]
    return f"ta-{side.lower()}-{clean}-{int(time.time())}-{uuid.uuid4().hex[:6]}"[:36]


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.environ.get(name, default))


def plan_from_decision(
    decision_text: str,
    symbol: str,
    *,
    price: Decimal | None = None,
    max_order_amount: Decimal | None = None,
    min_quantity: Decimal = Decimal("1"),
) -> TossOrderPlan:
    rating = parse_rating(decision_text)
    broker_symbol = toss_symbol(symbol)
    if not min_quantity.is_finite() or min_quantity <= 0:
        raise ValueError("min_quantity must be finite and positive.")
    if price is not None and (not price.is_finite() or price <= 0):
        raise ValueError("price must be finite and positive.")
    if rating in BUY_RATINGS:
        budget = max_order_amount if max_order_amount is not None else _env_decimal("TRADINGAGENTS_TOSS_MAX_ORDER_AMOUNT", "100000")
        if not budget.is_finite() or budget <= 0:
            raise ValueError("Order budget must be finite and positive.")
        quantity = None
        order_amount = None
        if price and price > 0:
            quantity_value = (budget / price).to_integral_value(rounding=ROUND_DOWN)
            if quantity_value < min_quantity:
                return TossOrderPlan(
                    action="HOLD",
                    rating=rating,
                    symbol=broker_symbol,
                    reason=f"Budget {budget} is below one-share price {price}.",
                )
            quantity = str(quantity_value)
        else:
            order_amount = str(budget)
        return TossOrderPlan(
            action="BUY",
            rating=rating,
            symbol=broker_symbol,
            quantity=quantity,
            order_amount=order_amount,
            reason=f"Portfolio rating is {rating}.",
        )
    if rating in SELL_RATINGS:
        return TossOrderPlan(
            action="SELL",
            rating=rating,
            symbol=broker_symbol,
            quantity=str(min_quantity),
            reason=f"Portfolio rating is {rating}; default sell quantity is {min_quantity}.",
        )
    return TossOrderPlan(
        action="HOLD",
        rating=rating,
        symbol=broker_symbol,
        reason=f"Portfolio rating is {rating}.",
    )


def order_payload(plan: TossOrderPlan) -> dict[str, Any]:
    if plan.action not in {"BUY", "SELL"}:
        return {}
    payload: dict[str, Any] = {
        "clientOrderId": _client_order_id(plan.action, plan.symbol),
        "symbol": plan.symbol,
        "side": plan.action,
        "orderType": plan.order_type,
        "timeInForce": plan.time_in_force,
    }
    if plan.quantity:
        payload["quantity"] = plan.quantity
    if plan.order_amount:
        payload["orderAmount"] = plan.order_amount
    return payload


def _default_paper_trade_path() -> Path:
    return Path(os.environ.get(
        "TRADINGAGENTS_PAPER_TRADE_LOG",
        str(Path(__file__).resolve().parents[2] / ".local" / "paper_trades" / "trades.jsonl"),
    ))


def record_paper_trade(
    plan: TossOrderPlan,
    payload: dict[str, Any],
    *,
    log_path: str | Path | None = None,
    experience_run_id: str | None = None,
    simulation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(log_path) if log_path else _default_paper_trade_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "paperTradeId": f"paper-{int(time.time())}-{uuid.uuid4().hex[:8]}",
        "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        # An order intent has no fill price, cash ledger, or position accounting.
        "status": "RECORDED",
        "fillVerified": False,
        "mode": "dry-run",
        "experienceRunId": experience_run_id,
        "plan": plan.__dict__,
        "payload": payload,
        "simulation": simulation,
    }
    if simulation:
        record["status"] = simulation["status"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"path": str(path), "record": record}


async def execute_plan(
    plan: TossOrderPlan,
    *,
    execute: bool = False,
    timeout: float = 10.0,
    paper_trade_log: str | Path | None = None,
    live_order_log: str | Path | None = None,
    experience_run_id: str | None = None,
    paper_account_path: str | Path | None = None,
    paper_capital: str = "10000000",
) -> dict[str, Any]:
    checkpoint("order-preparation")
    payload = order_payload(plan)
    if not payload:
        return {"executed": False, "dryRun": not execute, "plan": plan.__dict__, "payload": payload}
    if not execute:
        simulation = None
        if paper_account_path:
            from .account_service import fetch_quotes
            from .paper_account import PaperAccount
            account = PaperAccount(paper_account_path)
            account.initialize(paper_capital,
                fee_rate=os.getenv("TRADINGAGENTS_PAPER_FEE_RATE", "0"),
                tax_rate=os.getenv("TRADINGAGENTS_PAPER_SELL_TAX_RATE", "0"),
                slippage=os.getenv("TRADINGAGENTS_PAPER_SLIPPAGE_RATE", "0"))
            try:
                prices = await fetch_quotes([plan.symbol])
            except Exception:
                prices = {}
            checkpoint("paper-fill")
            simulation = account.fill(plan, prices.get(plan.symbol), experience_run_id or payload["clientOrderId"])
        if simulation is None:
            checkpoint("paper-record")
        paper = record_paper_trade(plan, payload, log_path=paper_trade_log,
            experience_run_id=experience_run_id, simulation=simulation)
        return {
            "executed": False,
            "recorded": True,
            "simulated": True,
            "dryRun": True,
            "plan": plan.__dict__,
            "payload": payload,
            "paperTrade": paper,
            "paperExecution": simulation,
        }

    client_id = os.environ.get("TOSS_CLIENT_ID")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET")
    account_seq = os.environ.get("TOSS_ACCOUNT_SEQ")
    if not client_id or not client_secret or not account_seq:
        raise ValueError("TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, and TOSS_ACCOUNT_SEQ are required.")

    path = Path(live_order_log) if live_order_log else Path(__file__).resolve().parents[2] / ".local" / "live_orders" / "orders.jsonl"

    def record(status: str, **extra: Any) -> dict[str, Any]:
        entry = {
            "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "live", "status": status, "fillVerified": False,
            "experienceRunId": experience_run_id,
            "plan": plan.__dict__, "payload": payload, **extra,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    checkpoint("order-submitting")
    record("SUBMITTING")
    try:
        async with TossClient(client_id, client_secret, account_seq=account_seq, timeout=timeout) as client:
            result = await client.create_order(payload)
    except RunStopped:
        record("CANCELLED_BEFORE_SUBMIT")
        raise
    except Exception as exc:
        # A lost response does not prove rejection. Leave the client order ID
        # available for reconciliation rather than automatically sending again.
        record("UNCONFIRMED", error=str(exc))
        raise
    entry = record("SUBMITTED", result=result)
    return {"executed": True, "dryRun": False, "plan": plan.__dict__, "payload": payload, "result": result, "liveOrder": {"path": str(path), "record": entry}}
