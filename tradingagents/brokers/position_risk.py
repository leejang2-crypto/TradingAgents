"""Persistent, LLM-independent lifecycle management for this strategy's KRW fills.

Only broker-confirmed fills create managed live positions. Existing unrelated
holdings are observed but never silently adopted. Unknown submissions are not retried.
"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from .account_service import credentials, fetch_quotes, live_cache_path, paper_account
from .paper_account import checked_quote, now_kst, number, KST
from .toss_client import TossClient, OrderWindowClosed
from .toss_executor import TossOrderPlan, record_paper_trade
from .intraday import INTRADAY_DEFAULTS, evaluate_entry, session_candles, size_entry


RISK_DEFAULTS = {"position_check_seconds": 30, "liquidation_minutes": 15, "buy_cutoff_minutes": 30}
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "SIMULATED_FILLED", "SIMULATED_REJECTED", "NOT_SENT"}


def iso_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Missing timezone")
    return stamp.astimezone(KST)


class RiskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()

    @contextmanager
    def locked(self):
        with self.mutex, self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            row = db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        self.path.chmod(0o600)
        return json.loads(row[0]) if row else {"positions": {}, "orders": {}, "days": {}, "cashDelta": "0", "events": []}

    def save(self, state):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(state, ensure_ascii=False),))


class BrokerBackend:
    def __init__(self, local_dir, live, capital, clock=now_kst):
        self.local_dir, self.live, self.capital = Path(local_dir), live, capital
        self.clock = clock

    def call(self, method, *args, **kwargs):
        valid_until = kwargs.pop("_valid_until", None)
        async def invoke():
            client_id, secret = credentials()
            async with TossClient(client_id, secret, account_seq=os.getenv("TOSS_ACCOUNT_SEQ")) as client:
                client.order_valid_until = valid_until
                return await getattr(client, method)(*args, **kwargs)
        return asyncio.run(invoke())

    def calendar(self, day):
        return self.call("get_kr_calendar", day)

    def holdings(self):
        if not self.live:
            return {row["symbol"]: number(row["quantity"]) for row in paper_account(self.local_dir, self.capital).view()["positions"]}
        payload = self.call("get_holdings")
        if not isinstance(payload.get("items"), list):
            raise ValueError("Invalid holdings response")
        return {row["symbol"]: number(row["quantity"]) for row in payload["items"]
                if row.get("currency") == "KRW" and row.get("marketCountry") == "KR"}

    def prices(self, symbols):
        return asyncio.run(fetch_quotes(list(symbols)))

    def entry_signal(self, symbol, start, now, settings):
        rows = session_candles(self.call, symbol, start, now)
        markets = {s: session_candles(self.call, s, start, now, index=True) for s in ("KOSPI", "KOSDAQ")}
        book = self.call("get_orderbook", symbol)
        return evaluate_entry(symbol, rows, book, markets, start, self.clock(), settings)

    def purchase_basis(self, symbol, quantity):
        # Only full-account adoption may use the broker's aggregate purchase price.
        payload = self.call("get_holdings")
        for row in payload["items"]:
            if row["symbol"] == symbol and row.get("currency") == "KRW" and number(row["quantity"]) == quantity:
                return number(row["averagePurchasePrice"])
        raise ValueError("Purchase price/quantity could not be verified")

    def open_orders(self):
        if not self.live:
            return []
        payload = self.call("get_orders", status="OPEN")
        if payload.get("hasNext") is not False or not isinstance(payload.get("orders"), list):
            raise ValueError("Incomplete open order snapshot")
        return payload["orders"]

    def sellable(self, symbol):
        return number(self.call("get_sellable_quantity", symbol)["sellableQuantity"]) if self.live else self.holdings().get(symbol, Decimal(0))

    def affordable_quantity(self, price):
        if self.live:
            raise ValueError("Paper buying power requested for live account")
        return paper_account(self.local_dir, self.capital).affordable_quantity(price)

    def submit(self, payload, now, *, valid_until=None):
        from tradingagents.monitoring.control import checkpoint
        if self.live:
            return self.call("create_order", payload, _valid_until=valid_until)
        plan = TossOrderPlan(payload["side"], "Buy" if payload["side"] == "BUY" else "Sell", payload["symbol"],
                             quantity=payload.get("quantity"), order_amount=payload.get("orderAmount"), order_type=payload["orderType"])
        prices = self.prices([plan.symbol])
        if payload.get("price"):
            price = checked_quote(prices.get(plan.symbol), plan.symbol, self.clock(), max_age=120)
            account = paper_account(self.local_dir, self.capital)
            # A dry run does not invent a fill above a live BUY limit price.
            with account.connect() as db:
                slip = number(db.execute("SELECT slippage FROM account WHERE id=1").fetchone()[0])
            if price * (1 + slip) > number(payload["price"]):
                raise OrderWindowClosed("지정가보다 시세가 높아 모의 매수를 체결하지 않았습니다.")
        checkpoint("paper-fill")
        if valid_until and self.clock() >= valid_until:
            raise OrderWindowClosed("모의 체결 전 주문 제한 시간이 지났습니다.")
        fill = paper_account(self.local_dir, self.capital).fill(plan, prices.get(plan.symbol), payload["clientOrderId"],
                                                              now=self.clock(), limit_price=payload.get("price"))
        record_paper_trade(plan, payload, simulation=fill, log_path=self.local_dir / "paper_trades/trades.jsonl")
        return {"orderId": payload["clientOrderId"], "paperFill": fill}

    def detail(self, order):
        if self.live:
            return self.call("get_order", order["orderId"])
        account = paper_account(self.local_dir, self.capital)
        with account.connect() as db:
            row = db.execute("SELECT payload FROM fills WHERE id=?", (order["payload"]["clientOrderId"],)).fetchone()
        if not row:
            raise ValueError("Paper fill not found")
        fill = json.loads(row["payload"])
        qty = number(fill.get("quantity", 0))
        return {"orderId": order["orderId"], "symbol": fill["symbol"], "side": fill["side"], "currency": "KRW",
                "status": fill["status"], "orderedAt": fill["recordedAt"], "execution": {
                    "filledQuantity": str(qty), "filledAmount": str(qty * number(fill.get("price", 0))),
                    "commission": fill.get("fee", "0"), "tax": fill.get("tax", "0"), "filledAt": fill["recordedAt"]}}

    def cancel(self, order_id):
        return self.call("cancel_order", order_id)


class PositionRiskManager:
    def __init__(self, local_dir, *, live=False, settings=None, backend=None, clock=now_kst):
        self.local_dir, self.live = Path(local_dir), live
        self.settings = settings or (lambda: {})
        self.clock = clock
        name = live_cache_path(local_dir).stem if live else "paper"
        self.store = RiskStore(self.local_dir / "positions" / (name + ".sqlite3"))
        self.backend = backend or BrokerBackend(local_dir, live, self.settings().get("paper_capital", "10000000"), clock)
        self.stop_event = threading.Event()
        self.thread = None
        self.calendars = {}

    def _event(self, state, kind, **fields):
        state["events"].append({"at": self.clock().isoformat(), "kind": kind, **fields})
        state["events"] = state["events"][-200:]

    def _journal(self, state, order):
        if not self.live:
            return
        payload = order["payload"]
        status = {"UNKNOWN": "UNCONFIRMED", "NOT_SENT": "CANCELLED_BEFORE_SUBMIT"}.get(order["status"], order["status"])
        row = {"recordedAt": self.clock().isoformat(), "mode": "live", "status": status,
               "fillVerified": order["status"] == "FILLED", "experienceRunId": order.get("experienceRunId"),
               "accountKey": self.store.path.stem, "payload": payload,
               "plan": {"action": payload["side"], "symbol": payload["symbol"], "quantity": payload.get("quantity"),
                        "order_amount": payload.get("orderAmount"), "reason": order.get("reason"), "rating": payload["side"].title()},
               "result": {"orderId": order.get("orderId"), "status": order["status"], "execution": order.get("detail", {}).get("execution")}}
        try:
            path = self.local_dir / "live_orders/orders.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            state["journalError"] = None
        except OSError as exc:
            # The risk database is authoritative and was committed before the side effect.
            state["journalError"] = type(exc).__name__

    def _calendar(self, day):
        key = day.isoformat()
        cached = self.calendars.get(key)
        if not cached or (self.clock() - cached[0]).total_seconds() >= 300:
            data = self.backend.calendar(key)
            if data["today"]["date"] != key:
                raise ValueError("Calendar date mismatch")
            self.calendars[key] = (self.clock(), data)
        return self.calendars[key][1]

    def _window(self, now, settings):
        calendar = self._calendar(now.date())
        session = (calendar["today"].get("integrated") or {}).get("regularMarket")
        if not session or now.date().isoformat() in {v.strip() for v in settings.get("holiday_dates", "").split(",")}:
            return None
        start, end = iso_time(session["startTime"]), iso_time(session["endTime"])
        configured_start = datetime.combine(now.date(), datetime.strptime(settings.get("market_open", "09:00"), "%H:%M").time(), KST)
        configured_end = datetime.combine(now.date(), datetime.strptime(settings.get("market_close", "15:30"), "%H:%M").time(), KST)
        return max(start, configured_start), min(end, configured_end)

    def _deadlines(self, position):
        acquired = date.fromisoformat(position["acquiredDate"])
        day2 = date.fromisoformat(self._calendar(acquired)["nextBusinessDay"]["date"])
        if day2 <= acquired:
            raise ValueError("Invalid next trading day")
        position["maxHoldingDate"] = day2.isoformat()
        return day2 if position.get("carryApproved") else acquired

    def _import(self, state):
        if self.live and state.get("historyImported"):
            return
        if self.live:
            path = self.local_dir / "live_orders/orders.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            # Old journals have no account identity. Never assign them to today's account automatically.
            if rows:
                self._event(state, "legacy-live-history-needs-adoption", message="기존 실계좌 보유분은 자동 편입하지 않습니다.")
        else:
            account = paper_account(self.local_dir, self.settings().get("paper_capital", "10000000"))
            with account.connect() as db:
                rows = [json.loads(row["payload"]) for row in db.execute("SELECT payload FROM fills ORDER BY recorded_at,rowid")]
            imported = deepcopy(state)
            for fill in rows:
                record = imported["orders"].get(fill["id"])
                if record and record["status"] in TERMINAL and not record.get("financialsIncomplete"):
                    continue
                if record is None:
                    payload = {"clientOrderId": fill["id"], "symbol": fill["symbol"], "side": fill["side"]}
                    record = {"payload": payload, "orderId": fill["id"], "status": "PENDING", "createdAt": fill["recordedAt"], "appliedQty": "0", "appliedCash": "0"}
                # A committed paper fill also recovers a lost local acknowledgement.
                record["orderId"] = fill["id"]
                imported["orders"][fill["id"]] = record
                self._apply_detail(imported, record, self.backend.detail(record))
            state.update(imported)
        state["historyImported"] = True
        self.store.save(state)

    def _apply_detail(self, state, order, detail):
        payload = order["payload"]
        if any(detail.get(key) != payload[key] for key in ("symbol", "side")) or detail.get("orderId") != order["orderId"] or detail.get("currency") != "KRW":
            raise ValueError("Order detail identity mismatch")
        execution = detail["execution"]
        filled = number(execution["filledQuantity"])
        prior = number(order.get("appliedQty", 0))
        if filled < prior or filled < 0 or filled != filled.to_integral_value():
            raise ValueError("Invalid cumulative filled quantity")
        if detail["status"] in {"FILLED", "SIMULATED_FILLED"} and filled <= 0:
            raise ValueError("Completed order has no confirmed fills")
        if payload.get("quantity") and filled > number(payload["quantity"]):
            raise ValueError("Fill exceeds requested quantity")
        amount = number(execution.get("filledAmount") or 0)
        fee, tax = number(execution.get("commission") or 0), number(execution.get("tax") or 0)
        if filled and not execution.get("filledAt"):
            raise ValueError("Missing fill timestamp")
        financials_complete = not filled or all(execution.get(key) is not None for key in ("filledAmount", "commission", "tax"))
        if amount < 0 or fee < 0 or tax < 0 or (filled > 0 and execution.get("filledAmount") is not None and amount <= 0):
            raise ValueError("Invalid fill amounts")
        signed_cash = -(amount + fee + tax) if payload["side"] == "BUY" else amount - fee - tax
        if financials_complete:
            state["cashDelta"] = str(number(state["cashDelta"]) + signed_cash - number(order.get("appliedCash", 0)))
        delta = filled - prior
        symbol = payload["symbol"]
        if delta:
            position = state["positions"].get(symbol)
            if payload["side"] == "BUY":
                acquired = iso_time(detail.get("orderedAt") or execution["filledAt"]).date().isoformat()
                if not position or number(position["quantity"]) == 0:
                    position = {"symbol": symbol, "quantity": "0", "acquiredDate": acquired, "carryApproved": False,
                                "cycle": uuid.uuid4().hex, "liquidate": False, "entryRunId": order.get("experienceRunId")}
                    state["positions"][symbol] = position
                position["quantity"] = str(number(position["quantity"]) + delta)
                position["acquiredDate"] = min(position["acquiredDate"], acquired)
                if order.get("entryStopPrice"):
                    position["entryStopPrice"] = order["entryStopPrice"]
            else:
                if not position or number(position["quantity"]) < delta:
                    raise ValueError("Sell fills exceed strategy holdings")
                position["quantity"] = str(number(position["quantity"]) - delta)
                if number(position["quantity"]) == 0:
                    position["closedAt"] = execution["filledAt"]
                    state.setdefault("closedCycles", []).append({"cycle": position["cycle"], "symbol": symbol,
                        "at": execution["filledAt"], "reason": position.get("exitReason", order.get("reason", "agent-sell"))})
                    state["closedCycles"] = state["closedCycles"][-500:]
            self._event(state, "fill-confirmed", symbol=symbol, side=payload["side"], quantity=str(delta), orderId=order["orderId"], simulated=not self.live)
        order.update(status=detail["status"], appliedQty=str(filled), detail=detail, financialsIncomplete=not financials_complete)
        if financials_complete:
            order["appliedCash"] = str(signed_cash)
        if order["status"] in {"FILLED", "SIMULATED_FILLED"} and payload.get("quantity") and filled != number(payload["quantity"]):
            raise ValueError("FILLED status does not match requested quantity")

    def _reconcile(self, state):
        for order in state["orders"].values():
            if order["status"] in TERMINAL and not order.get("financialsIncomplete"):
                continue
            if not order.get("orderId"):
                order["status"] = "UNKNOWN"
                continue
            # Work on a copy: invalid remote evidence must not partially update the ledger.
            copy = deepcopy(state)
            key = order["payload"]["clientOrderId"]
            self._apply_detail(copy, copy["orders"][key], self.backend.detail(order))
            state.update(copy)
            self.store.save(state)
            current = state["orders"][key]
            if any(current.get(k) != order.get(k) for k in ("status", "appliedQty", "appliedCash", "financialsIncomplete")):
                self._journal(state, current)
                self.store.save(state)

    def _pending(self, state, symbol=None):
        return [order for order in state["orders"].values() if order["status"] not in TERMINAL
                and (symbol is None or order["payload"]["symbol"] == symbol)]

    def _cancel(self, state, order):
        if not self.live or not order.get("orderId") or order.get("cancelRequested"):
            return
        order["cancelRequested"] = self.clock().isoformat()
        self.store.save(state)
        try:
            self.backend.cancel(order["orderId"])
        except Exception as exc:
            self._event(state, "cancel-needs-confirmation", orderId=order["orderId"], error=type(exc).__name__)
        # A cancel acknowledgement is not proof that remaining shares did not fill.
        self.store.save(state)

    def _submit(self, state, payload, reason, experience_id=None, entry_stop=None, entry_valid_until=None):
        from tradingagents.monitoring.control import RunStopped
        key = payload["clientOrderId"]
        record = {"payload": payload, "status": "SUBMITTING", "createdAt": self.clock().isoformat(),
                  "reason": reason, "experienceRunId": experience_id, "appliedQty": "0", "appliedCash": "0"}
        if entry_stop is not None:
            record["entryStopPrice"] = entry_stop
        if not self.live:
            record["orderId"] = key
        state["orders"][key] = record
        self.store.save(state)
        self._journal(state, record)
        try:
            settings = {**RISK_DEFAULTS, **self.settings()}
            window = self._window(self.clock(), settings)
            if not window:
                raise OrderWindowClosed("정규장 주문 시간이 아닙니다.")
            deadline = window[1] - timedelta(minutes=int(settings["buy_cutoff_minutes"])) if payload["side"] == "BUY" else window[1]
            if entry_valid_until:
                deadline = min(deadline, entry_valid_until)
            if not window[0] <= self.clock() < deadline:
                raise OrderWindowClosed("주문 제한 시간이 지났습니다.")
            response = self.backend.submit(payload, self.clock(), valid_until=deadline)
            if not isinstance(response.get("orderId"), str) or not response["orderId"]:
                raise ValueError("Missing order ID")
            record.update(orderId=response["orderId"], status="PENDING")
        except (RunStopped, OrderWindowClosed):
            record.update(status="NOT_SENT")
            self._journal(state, record)
            self.store.save(state)
            raise
        except Exception as exc:
            record.update(status="UNKNOWN", error=type(exc).__name__)
        self.store.save(state)
        self._journal(state, record)
        self.store.save(state)
        return record

    def _mark_day(self, state, now, prices, settings):
        value = number(state["cashDelta"])
        for symbol, position in state["positions"].items():
            if number(position["quantity"]) > 0:
                value += number(position["quantity"]) * checked_quote(prices.get(symbol), symbol, now, max_age=120)
        key = now.date().isoformat()
        if key not in state["days"]:
            previous = [v for k, v in sorted(state["days"].items()) if k < key]
            prior_business_day = self._calendar(now.date())["previousBusinessDay"]["date"]
            prior_valid = bool(previous and iso_time(previous[-1]["asOf"]).date().isoformat() == prior_business_day)
            state["days"][key] = {"baseline": previous[-1]["value"] if prior_valid else str(value),
                                  "capital": str(number(settings.get("paper_capital", "10000000"))),
                                  "targetReached": False, "baselineAt": previous[-1]["asOf"] if prior_valid else now.isoformat(),
                                  "partial": not prior_valid if previous
                                  else any(number(p["quantity"]) > 0 for p in state["positions"].values())}
        day = state["days"][key]
        pnl = value - number(day["baseline"])
        day.update(value=str(value), pnl=str(pnl), returnPct=str(pnl / number(day["capital"]) * 100), asOf=now.isoformat())
        # The former account-wide profit target is intentionally no longer a trigger.
        day["targetReached"] = False
        if pnl <= -number(day["capital"]) * number(settings["daily_loss_limit"]):
            day["lossLimitReached"] = True
        consecutive = 0
        for trade in reversed(state.get("closedCycles", [])):
            if iso_time(trade["at"]).date() != now.date() or trade["reason"] != "stop-loss":
                break
            consecutive += 1
        day["consecutiveStops"] = consecutive
        if consecutive >= int(settings["max_consecutive_stops"]):
            day["stopLimitReached"] = True
        return day

    def _position_basis(self, state, position):
        """Replay confirmed cumulative fills, including legacy journals and fees.

        New entries never pyramid, so a partially filled buy cannot overlap a
        new buy cycle. Unknown legacy/adopted bases are not replaced with quotes.
        """
        seed = position.get("basisSeed")
        if not seed and position.get("acquisitionSource") == "user-confirmed":
            if self._pending(state, position["symbol"]):
                raise ValueError("기존 보유분 원가 확인 전 진행 주문 확인 필요")
            average = self.backend.purchase_basis(position["symbol"], number(position["quantity"]))
            if average <= 0:
                raise ValueError("Invalid broker purchase basis")
            seed = {"quantity": position["quantity"], "averagePrice": str(average), "at": self.clock().isoformat(),
                    "existingOrderIds": list(state["orders"])}
            position["basisSeed"] = seed
        qty = number(seed["quantity"]) if seed else Decimal(0)
        gross = qty * number(seed["averagePrice"]) if seed else Decimal(0)
        cost = gross
        acquired_at = seed["at"] if seed else None
        for order in sorted(state["orders"].values(), key=lambda o: o["createdAt"]):
            if order["payload"]["symbol"] != position["symbol"] or (seed and order["payload"]["clientOrderId"] in seed["existingOrderIds"]):
                continue
            filled = number(order.get("appliedQty", 0))
            if not filled:
                continue
            if order.get("financialsIncomplete"):
                raise ValueError("매입 원가 체결 정보 미확인")
            execution = order["detail"]["execution"]
            amount = number(execution["filledAmount"])
            if order["payload"]["side"] == "BUY":
                if qty == 0:
                    acquired_at = execution["filledAt"]
                qty += filled
                gross += amount
                cost += amount + number(execution["commission"]) + number(execution["tax"])
            else:
                if filled > qty:
                    raise ValueError("매입 원가 이력 불일치")
                gross *= (qty - filled) / qty
                cost *= (qty - filled) / qty
                qty -= filled
        if qty <= 0 or qty != number(position["quantity"]) or gross <= 0:
            raise ValueError("매입 원가 확인 필요")
        position.update(averagePrice=str(gross / qty), costPerShare=str(cost / qty), acquiredAt=acquired_at)

    def _price_exit(self, state, position, prices, now, settings):
        try:
            self._position_basis(state, position)
            price = checked_quote(prices.get(position["symbol"]), position["symbol"], now, max_age=120)
            average = number(position["averagePrice"])
            target = average * (1 + number(settings["position_target_return"]))
            stop = average * (1 - number(settings["position_stop_loss"]))
            # Preserve the entry's tighter ATR/previous-low stop, never loosen it.
            if position.get("entryStopPrice"):
                stop = max(stop, number(position["entryStopPrice"]))
            basis = number(position["costPerShare"])
            # The round-trip estimate already includes entry costs; avoid counting them twice.
            remaining_cost = max(Decimal(0), average * number(settings["estimated_cost_rate"]) - (basis - average))
            position.update(lastPrice=str(price), returnPct=str((price / average - 1) * 100),
                            targetPrice=str(target), stopPrice=str(stop), basisError=None,
                            estimatedNetReturnPct=str(((price - remaining_cost) / basis - 1) * 100))
            if price <= stop:
                return "stop-loss"
            if price >= target:
                return "position-target"
            elapsed = (now - iso_time(position["acquiredAt"])).total_seconds() / 60
            if elapsed >= int(settings["stagnation_minutes"]) and price < average * Decimal("1.002"):
                return "time-exit"
        except Exception as exc:
            position["basisError"] = str(exc)
            position.update(lastPrice=None, returnPct=None, estimatedNetReturnPct=None)
        return None

    def _remaining_limits(self, state, settings):
        capital = number(settings.get("paper_capital", "10000000"))
        cost_rate = number(settings["estimated_cost_rate"])
        used, risk = Decimal(0), Decimal(0)
        for p in state["positions"].values():
            qty = number(p["quantity"])
            if qty <= 0:
                continue
            if p.get("basisError") or not p.get("lastPrice"):
                raise ValueError("기존 보유분 원가/위험 미확인")
            price = number(p["lastPrice"])
            used += qty * number(p["costPerShare"])
            risk += qty * (max(Decimal(0), price - number(p["stopPrice"])) + price * cost_rate)
        for order in self._pending(state):
            payload = order["payload"]
            if payload["side"] != "BUY":
                continue
            if not payload.get("price") or not payload.get("quantity"):
                raise ValueError("기존 매수 주문 위험 미확인")
            qty = max(Decimal(0), number(payload["quantity"]) - number(order.get("appliedQty", 0)))
            price = number(payload["price"])
            used += qty * price * (1 + cost_rate)
            risk += qty * price * (number(settings["position_stop_loss"]) + cost_rate)
        daily_pnl = number(state.get("daily", {}).get("pnl", 0))
        return {"available_capital": str(capital - used),
                "available_risk": str(capital * number(settings["daily_loss_limit"]) + min(Decimal(0), daily_pnl) - risk)}

    def _tick(self, state, *, orders=True):
        self._import(state)
        self._reconcile(state)
        settings = {**RISK_DEFAULTS, **INTRADAY_DEFAULTS, **self.settings()}
        now = self.clock()
        window = self._window(now, settings)
        state.update(checkedAt=now.isoformat(), marketOpen=bool(window and window[0] <= now < window[1]), error=None)
        state["unconfirmed"] = [o["payload"]["clientOrderId"] for o in self._pending(state) if not o.get("orderId")]
        if not state["marketOpen"]:
            self.store.save(state)
            return
        held = self.backend.holdings()
        state["unmanaged"] = {s: str(q - number(state["positions"].get(s, {}).get("quantity", 0))) for s, q in held.items()
                              if q > number(state["positions"].get(s, {}).get("quantity", 0))}
        close = window[1] - timedelta(minutes=int(settings["liquidation_minutes"]))
        cutoff = window[1] - timedelta(minutes=int(settings["buy_cutoff_minutes"]))
        state.update(liquidationAt=close.isoformat(), buyCutoffAt=cutoff.isoformat())
        symbols = [s for s, p in state["positions"].items() if number(p["quantity"]) > 0]
        prices = {}
        try:
            prices = self.backend.prices(symbols)
            if any(o.get("financialsIncomplete") for o in state["orders"].values()):
                raise ValueError("Fill fees/amounts not yet confirmed")
            if any(held.get(s, Decimal(0)) < number(state["positions"][s]["quantity"]) for s in symbols):
                raise ValueError("Holdings differ from strategy ledger")
            day = self._mark_day(state, now, prices, settings)
            state["valuationError"] = None
        except Exception as exc:
            day = state["days"].get(now.date().isoformat(), {})
            state["valuationError"] = type(exc).__name__
        state["daily"] = day
        open_orders = self.backend.open_orders()
        state["unconfirmed"] = [o["payload"]["clientOrderId"] for o in self._pending(state) if not o.get("orderId")]
        for order in self._pending(state):
            age = (now - iso_time(order["createdAt"])).total_seconds()
            position = state["positions"].get(order["payload"]["symbol"], {})
            if orders and ((order["payload"]["side"] == "BUY" and (now >= cutoff or day.get("lossLimitReached") or day.get("stopLimitReached") or position.get("liquidate") or age >= 90)) or
                           (order["payload"]["side"] == "SELL" and age >= 90)):
                self._cancel(state, order)
        for symbol in symbols:
            p = state["positions"][symbol]
            deadline = self._deadlines(p)
            reason = self._price_exit(state, p, prices, self.clock(), settings) if held.get(symbol, 0) >= number(p["quantity"]) else None
            if day.get("lossLimitReached"):
                reason = "daily-loss-limit"
            if now.date() > deadline or (now.date() == deadline and now >= close):
                reason = reason or "holding-deadline"
            if reason and not p["liquidate"]:
                p["liquidate"] = True
                p["exitReason"] = reason
            if orders and p["liquidate"]:
                for pending in self._pending(state, symbol):
                    if pending["payload"]["side"] == "BUY":
                        self._cancel(state, pending)
            qty = number(p["quantity"])
            p["observedQuantity"] = str(held.get(symbol, 0))
            p["alert"] = "계좌 수량과 관리 수량 불일치" if held.get(symbol, 0) < qty else p.get("basisError")
            if not orders or not p["liquidate"]:
                continue
            # New orders are blocked by any unknown submission, including other symbols.
            if state["unconfirmed"] or self._pending(state, symbol) or any(o.get("symbol") == symbol for o in open_orders):
                p["alert"] = "기존 주문 체결/취소 확인 대기"
                continue
            attempts = [o for o in state["orders"].values() if o["payload"]["symbol"] == symbol and
                        o["payload"]["side"] == "SELL" and iso_time(o["createdAt"]).date() == now.date()]
            if len(attempts) >= 3:
                p["alert"] = "오늘 청산 주문 3회 시도. 수동 확인 필요"
                continue
            sellable = min(qty, held.get(symbol, Decimal(0)), self.backend.sellable(symbol))
            if sellable <= 0 or sellable != sellable.to_integral_value():
                p["alert"] = "매도 가능한 수량 없음"
                continue
            current = self.clock()
            if not window[0] <= current < window[1]:
                p["alert"] = "장 종료: 다음 거래일 재점검"
                break
            self._submit(state, {"clientOrderId": "ta-exit-" + uuid.uuid4().hex[:24], "symbol": symbol,
                                "side": "SELL", "quantity": str(sellable), "orderType": "MARKET", "timeInForce": "DAY"}, p["exitReason"], p.get("entryRunId"))
        self.store.save(state)

    def tick(self):
        with self.store.locked():
            state = self.store.load()
            try:
                self._tick(state)
            except Exception as exc:
                state.update(error=f"보유 관리 점검 실패 ({type(exc).__name__}): {exc}", checkedAt=self.clock().isoformat())
                self.store.save(state)

    def execute(self, plan, experience_id=None):
        import re
        from tradingagents.monitoring.control import checkpoint
        signal, signal_error, signal_settings = None, None, None
        if plan.action == "BUY":
            # Slow chart reads must not hold the position ledger lock and delay exits.
            try:
                settings = {**RISK_DEFAULTS, **INTRADAY_DEFAULTS, **self.settings()}
                now = self.clock()
                window = self._window(now, settings)
                if window and window[0] <= now < window[1] - timedelta(minutes=int(settings["buy_cutoff_minutes"])):
                    regular_start = iso_time(self._calendar(now.date())["today"]["integrated"]["regularMarket"]["startTime"])
                    signal_settings = {k: settings[k] for k in INTRADAY_DEFAULTS}
                    signal = self.backend.entry_signal(plan.symbol, regular_start, now, settings)
                else:
                    signal_error = "정규장 매수 시간 밖"
            except Exception as exc:
                signal_error = str(exc)
        with self.store.locked():
            state = self.store.load()
            self._tick(state, orders=False)
            checkpoint("position-risk-check")
            if plan.action == "HOLD":
                return {"executed": False, "dryRun": not self.live, "plan": plan.__dict__}
            now = self.clock()
            symbol = plan.symbol
            if not re.fullmatch(r"[A-Z0-9]{6}", symbol):
                raise ValueError("KR strategy requires a six-character symbol")
            day = state.get("daily", {})
            if not state.get("marketOpen"):
                raise ValueError("정규장 밖에서는 주문하지 않습니다.")
            if state.get("unconfirmed") or self._pending(state, symbol):
                raise ValueError("기존 주문 확인 전 신규 주문 차단")
            if any(o.get("symbol") == symbol for o in self.backend.open_orders()):
                raise ValueError("계좌에 같은 종목의 진행 중 주문이 있습니다.")
            position = state["positions"].get(symbol, {})
            if plan.action == "BUY":
                if now >= iso_time(state["buyCutoffAt"]) or day.get("lossLimitReached") or day.get("stopLimitReached") or state.get("valuationError"):
                    raise ValueError("매수 제한 시간, 일일 손실 제한 또는 평가자료 부족으로 신규 매수를 차단했습니다.")
                if number(position.get("quantity", 0)) > 0:
                    raise ValueError("보유 종목 추가 매수 금지")
                if not self.live and self.backend.holdings().get(symbol, Decimal(0)) > 0:
                    raise ValueError("모의계좌 보유 종목 추가 매수 금지")
                if position.get("closedAt") and iso_time(position["closedAt"]).date() == now.date():
                    raise ValueError("당일 청산 종목 재진입 금지")
                settings = {**INTRADAY_DEFAULTS, **self.settings()}
                try:
                    if signal_error or signal is None:
                        raise ValueError(signal_error or "진입 자료 없음")
                    if signal_settings != {k: settings[k] for k in INTRADAY_DEFAULTS}:
                        raise ValueError("분봉 조회 중 매매 설정 변경: 다시 분석하세요.")
                    if not signal["allowed"]:
                        raise ValueError("; ".join(signal["reasons"]))
                    if signal["symbol"] != symbol or not 0 <= (self.clock() - iso_time(signal["at"])).total_seconds() <= 30:
                        raise ValueError("진입 신호 유효기간 초과")
                    qty = size_entry(plan, signal, {**settings, **self._remaining_limits(state, settings)})
                    if not self.live:
                        qty = min(qty, self.backend.affordable_quantity(signal["ask"]))
                    if qty <= 0:
                        raise ValueError("현금 잔고/위험/투자금 한도로 1주도 매수할 수 없음")
                except Exception as exc:
                    self._event(state, "entry-blocked", symbol=symbol, reason=str(exc))
                    state.setdefault("entryChecks", {})[symbol] = {"allowed": False, "at": self.clock().isoformat(), "reasons": [str(exc)]}
                    self.store.save(state)
                    return {"ok": True, "executed": False, "dryRun": not self.live, "plan": plan.__dict__,
                            "skipped": "intraday-filter", "entryCheck": state["entryChecks"][symbol]}
                state.setdefault("entryChecks", {})[symbol] = signal
                payload = {"quantity": str(qty), "price": signal["ask"]}
            elif plan.action == "SELL":
                held = self.backend.holdings().get(symbol, Decimal(0))
                qty = min(number(position.get("quantity", 0)), held, self.backend.sellable(symbol))
                if qty <= 0:
                    return {"executed": False, "dryRun": not self.live, "plan": plan.__dict__, "skipped": "no-managed-holdings"}
                if qty != qty.to_integral_value():
                    raise ValueError("KR shares must have a whole quantity")
                payload = {"quantity": str(qty)}
                position.update(liquidate=True, exitReason="agent-sell")
            else:
                raise ValueError("Unsupported order side")
            payload.update(clientOrderId="ta-run-" + uuid.uuid4().hex[:24], symbol=symbol, side=plan.action, orderType="LIMIT" if plan.action == "BUY" else "MARKET", timeInForce="DAY")
            # Network reads above may cross the end of the regular session.
            window = self._window(self.clock(), {**RISK_DEFAULTS, **self.settings()})
            if not window or not window[0] <= self.clock() < window[1] or (plan.action == "BUY" and self.clock() >= iso_time(state["buyCutoffAt"])):
                raise ValueError("Order window closed during validation")
            checkpoint("order-submitting")
            order = self._submit(state, payload, "agent-decision", experience_id,
                                 entry_stop=signal["stopPrice"] if plan.action == "BUY" else None,
                                 entry_valid_until=iso_time(signal["at"]) + timedelta(seconds=30) if plan.action == "BUY" else None)
            self._reconcile(state)
            self.store.save(state)
            order = state["orders"][payload["clientOrderId"]]
            if order["status"] == "UNKNOWN":
                raise ValueError("주문 접수 결과 불확실. 자동 재전송하지 않습니다.")
            return {"executed": self.live, "dryRun": not self.live,
                    "ok": order["status"] not in {"REJECTED", "SIMULATED_REJECTED"},
                    "plan": {**plan.__dict__, "quantity": payload.get("quantity"), "order_amount": payload.get("orderAmount"), "order_type": payload["orderType"]}, "payload": payload,
                    "result": {"orderId": order.get("orderId"), "status": order["status"]},
                    "fillVerified": self.live and order["status"] == "FILLED", "simulated": not self.live,
                    "positionRisk": True, "paperExecution": {"status": order["status"]} if not self.live else None}

    def carry(self, symbol, cycle, allowed):
        if type(allowed) is not bool:
            raise ValueError("allowed must be boolean")
        with self.store.locked():
            state = self.store.load()
            p = state["positions"].get(symbol)
            if not p or p["cycle"] != cycle or number(p["quantity"]) <= 0:
                raise ValueError("보유 정보가 변경되었습니다. 새로 조회하세요.")
            now = self.clock()
            if allowed:
                if p["acquiredDate"] != now.date().isoformat() or p.get("liquidate"):
                    raise ValueError("매수 당일 청산 시작 전만 이월을 허용할 수 있습니다.")
                window = self._window(now, {**RISK_DEFAULTS, **self.settings()})
                if not window or now >= window[1] - timedelta(minutes=int(self.settings().get("liquidation_minutes", 15))):
                    raise ValueError("오늘 이월 승인 시간이 종료되었습니다.")
                self._deadlines(p)
                if self.clock() >= window[1] - timedelta(minutes=int(self.settings().get("liquidation_minutes", 15))):
                    raise ValueError("거래일 확인 중 이월 승인 시간이 종료되었습니다.")
            p["carryApproved"] = allowed
            self._event(state, "carry-setting", symbol=symbol, allowed=allowed, cycle=cycle)
            self.store.save(state)

    def adopt(self, symbol, acquired_date, confirmed):
        """Explicitly enroll legacy live holdings; do not infer their purchase date/cost."""
        import re
        if not self.live or confirmed is not True:
            raise ValueError("실계좌 기존 보유분 자동 청산 동의가 필요합니다.")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
            raise ValueError("국내 종목 코드 6자리를 입력하세요.")
        acquired = date.fromisoformat(acquired_date)
        now = self.clock()
        if acquired > now.date():
            raise ValueError("매수일은 미래 날짜일 수 없습니다.")
        if not (self._calendar(acquired)["today"].get("integrated") or {}).get("regularMarket"):
            raise ValueError("입력한 매수일은 국내 정규 거래일이 아닙니다.")
        with self.store.locked():
            state = self.store.load()
            self._import(state)
            self._reconcile(state)
            if number(state["positions"].get(symbol, {}).get("quantity", 0)) > 0 or self._pending(state):
                raise ValueError("이미 관리 중이거나 확인 중인 주문이 있습니다.")
            if any(o.get("symbol") == symbol for o in self.backend.open_orders()):
                raise ValueError("해당 종목의 진행 중 주문을 먼저 확인하세요.")
            qty = self.backend.holdings().get(symbol, Decimal(0))
            if qty <= 0 or qty != qty.to_integral_value():
                raise ValueError("편입 가능한 국내 보유 수량이 없습니다.")
            quote = self.backend.prices([symbol]).get(symbol)
            price = checked_quote(quote, symbol, now, max_age=120)
            position = {"symbol": symbol, "quantity": str(qty), "acquiredDate": acquired.isoformat(), "carryApproved": False,
                        "cycle": uuid.uuid4().hex, "liquidate": False, "acquisitionSource": "user-confirmed", "adoptedAt": now.isoformat()}
            self._deadlines(position)
            # Imported value is an external contribution, not instant strategy profit.
            state["cashDelta"] = str(number(state["cashDelta"]) - qty * price)
            state["positions"][symbol] = position
            state.get("unmanaged", {}).pop(symbol, None)
            if now.date().isoformat() in state["days"]:
                state["days"][now.date().isoformat()]["partial"] = True
            self._event(state, "legacy-holding-adopted", symbol=symbol, quantity=str(qty), acquiredDate=acquired.isoformat(), costBasis="adoption-mark-not-purchase-cost")
            self.store.save(state)

    def snapshot(self):
        with self.store.locked():
            state = self.store.load()
        return {**state, "mode": "live" if self.live else "dry-run", "maxTradingDays": 2, "enabled": True,
                "goalReturn": self.settings().get("position_target_return", ".01"), "goalScope": "position",
                "dailyLossLimit": self.settings().get("daily_loss_limit", ".02")}

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    # Corrupt/unwritable state also blocks execute(); never start from an empty ledger.
                    pass
                self.stop_event.wait(max(10, int(self.settings().get("position_check_seconds", 30))))
        self.thread = threading.Thread(target=loop, name="position-risk", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
