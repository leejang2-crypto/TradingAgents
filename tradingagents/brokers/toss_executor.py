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
    if rating in BUY_RATINGS:
        budget = max_order_amount or _env_decimal("TRADINGAGENTS_TOSS_MAX_ORDER_AMOUNT", "100000")
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
        "/Users/leejang2/Project/TradingAgents/.local/paper_trades/trades.jsonl",
    ))


def record_paper_trade(
    plan: TossOrderPlan,
    payload: dict[str, Any],
    *,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(log_path) if log_path else _default_paper_trade_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "paperTradeId": f"paper-{int(time.time())}-{uuid.uuid4().hex[:8]}",
        "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "FILLED",
        "mode": "dry-run",
        "plan": plan.__dict__,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"path": str(path), "record": record}


async def execute_plan(
    plan: TossOrderPlan,
    *,
    execute: bool = False,
    timeout: float = 10.0,
    paper_trade_log: str | Path | None = None,
) -> dict[str, Any]:
    payload = order_payload(plan)
    if not payload:
        return {"executed": False, "dryRun": True, "plan": plan.__dict__, "payload": payload}
    if not execute:
        paper = record_paper_trade(plan, payload, log_path=paper_trade_log)
        return {
            "executed": True,
            "simulated": True,
            "dryRun": True,
            "plan": plan.__dict__,
            "payload": payload,
            "paperTrade": paper,
        }

    client_id = os.environ.get("TOSS_CLIENT_ID")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET")
    account_seq = os.environ.get("TOSS_ACCOUNT_SEQ")
    if not client_id or not client_secret or not account_seq:
        raise ValueError("TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, and TOSS_ACCOUNT_SEQ are required.")

    async with TossClient(client_id, client_secret, account_seq=account_seq, timeout=timeout) as client:
        result = await client.create_order(payload)
    return {"executed": True, "dryRun": False, "plan": plan.__dict__, "payload": payload, "result": result}
