"""Cash-only KRW simulation ledger. No real broker orders or retroactive fills."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst():
    return datetime.now(KST)


def number(value):
    try:
        value = Decimal(str(value))
    except DecimalException as exc:
        raise ValueError("Invalid account number") from exc
    if not value.is_finite():
        raise ValueError("Non-finite account value")
    return value


def money(value):
    return format(value.quantize(Decimal("0.01")), "f")


def period_start(day, unit):
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    if unit == "year":
        return day.replace(month=1, day=1)
    raise ValueError("Invalid return period")


def checked_quote(quote, symbol, now, max_age=900):
    if not quote or quote.get("symbol") != symbol or quote.get("currency") != "KRW":
        raise ValueError("KRW quote is unavailable")
    price = number(quote["lastPrice"])
    stamp = datetime.fromisoformat(quote["timestamp"])
    if stamp.tzinfo is None or price <= 0:
        raise ValueError("Invalid quote price/time")
    age = (now - stamp).total_seconds()
    if age < -60 or age > max_age:
        raise ValueError("Quote is stale or in the future")
    return price


class PaperAccount:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), created_at TEXT NOT NULL,
                    initial_cash TEXT NOT NULL, cash TEXT NOT NULL,
                    realized TEXT NOT NULL, fee_rate TEXT NOT NULL,
                    tax_rate TEXT NOT NULL, slippage TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL, cost TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quotes (symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS valuations (
                    id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, capital, *, fee_rate="0", tax_rate="0", slippage="0", now=None):
        now = now or now_kst()
        capital = number(capital)
        rates = [number(v) for v in (fee_rate, tax_rate, slippage)]
        if capital <= 0 or any(v < 0 or v >= 1 for v in rates):
            raise ValueError("Invalid paper capital or cost assumptions")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM account").fetchone():
                db.execute("INSERT INTO account VALUES(1,?,?,?,?,?,?,?)", (
                    now.isoformat(), money(capital), money(capital), "0", *map(str, rates),
                ))
                self._snapshot(db, now)

    def symbols(self):
        with self.connect() as db:
            return [r["symbol"] for r in db.execute("SELECT * FROM positions") if number(r["quantity"]) > 0]

    def _store_quotes(self, db, quotes):
        for symbol, quote in quotes.items():
            if quote.get("symbol") != symbol or quote.get("currency") != "KRW":
                continue
            try:
                stamp = datetime.fromisoformat(quote["timestamp"])
                if stamp.tzinfo is None or number(quote["lastPrice"]) <= 0:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            old = db.execute("SELECT payload FROM quotes WHERE symbol=?", (symbol,)).fetchone()
            if old and datetime.fromisoformat(json.loads(old["payload"])["timestamp"]) > stamp:
                continue
            db.execute("INSERT OR REPLACE INTO quotes VALUES(?,?)", (symbol, json.dumps(quote)))

    def _snapshot(self, db, now, *, persist=True):
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if account is None:
            return {"initialized": False, "positions": [], "equity": None, "cash": None}
        positions, total, basis, complete = [], Decimal(0), Decimal(0), True
        for row in db.execute("SELECT * FROM positions ORDER BY symbol"):
            qty, cost = number(row["quantity"]), number(row["cost"])
            if qty <= 0:
                continue
            stored = db.execute("SELECT payload FROM quotes WHERE symbol=?", (row["symbol"],)).fetchone()
            quote = json.loads(stored["payload"]) if stored else {}
            # Last observed marks are shown, but stale marks cannot make a return.
            try:
                price = checked_quote(quote, row["symbol"], now)
                stale = False
            except (ValueError, KeyError, TypeError):
                price, stale, complete = None, True, False
            value = price * qty if price is not None else None
            if value is not None:
                total += value
            basis += cost
            positions.append({"symbol": row["symbol"], "quantity": str(qty),
                "averagePrice": money(cost / qty), "cost": money(cost),
                "lastPrice": str(price) if price is not None else None,
                "marketValue": money(value) if value is not None else None,
                "unrealizedPnl": money(value - cost) if value is not None else None,
                "quoteTime": quote.get("timestamp"), "stale": stale})
        cash = number(account["cash"])
        result = {"initialized": True, "mode": "dry-run", "currency": "KRW",
            "createdAt": account["created_at"], "asOf": now.isoformat(),
            "initialCapital": account["initial_cash"], "cash": money(cash),
            "equity": money(cash + total) if complete else None,
            "marketValue": money(total) if complete else None,
            "realizedPnl": account["realized"], "unrealizedPnl": money(total - basis) if complete else None,
            "valuationComplete": complete, "positions": positions,
            "assumptions": {"feeRate": account["fee_rate"], "sellTaxRate": account["tax_rate"], "slippageRate": account["slippage"]}}
        if persist:
            last = db.execute("SELECT payload FROM valuations ORDER BY id DESC LIMIT 1").fetchone()
            # Avoid hundreds of identical overnight/cash-only samples per day.
            if last:
                previous = json.loads(last["payload"])
                if previous["asOf"][:10] == result["asOf"][:10] and all(
                    previous.get(k) == v for k, v in result.items() if k != "asOf"
                ):
                    return result
            db.execute("INSERT INTO valuations(recorded_at,payload) VALUES(?,?)", (now.isoformat(), json.dumps(result)))
        return result

    def refresh(self, quotes, now=None):
        now = now or now_kst()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._store_quotes(db, quotes)
            view = self._snapshot(db, now, persist=False)
            # Do not replace a known observed valuation with an invented zero
            # or a new timestamp attached to an unavailable price.
            if view.get("valuationComplete"):
                self._snapshot(db, now)
            return view

    def view(self, now=None):
        with self.connect() as db:
            result = self._snapshot(db, now or now_kst(), persist=False)
            result["trades"] = [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM fills ORDER BY recorded_at DESC LIMIT 100")]
            return result

    def fill(self, plan, quote, execution_id, now=None):
        now = now or now_kst()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM fills WHERE id=?", (execution_id,)).fetchone()
            if old:
                return json.loads(old["payload"])
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            if account is None:
                raise ValueError("Paper account is not initialized")
            result = {"id": execution_id, "recordedAt": now.isoformat(), "symbol": plan.symbol,
                "side": plan.action, "status": "SIMULATED_REJECTED", "simulated": True, "fillVerified": False}
            try:
                if plan.action not in {"BUY", "SELL"} or not re.fullmatch(r"[A-Z0-9]{6}", plan.symbol):
                    raise ValueError("Paper fills support Korean BUY/SELL orders only")
                price = checked_quote(quote, plan.symbol, now)
                fee_rate, tax_rate, slip = (number(account[k]) for k in ("fee_rate", "tax_rate", "slippage"))
                price *= 1 + slip if plan.action == "BUY" else 1 - slip
                cash = number(account["cash"])
                existing = db.execute("SELECT * FROM positions WHERE symbol=?", (plan.symbol,)).fetchone()
                held, cost = (number(existing[k]) for k in ("quantity", "cost")) if existing else (Decimal(0), Decimal(0))
                if plan.quantity is not None:
                    qty = number(plan.quantity)
                elif plan.action == "BUY" and plan.order_amount:
                    qty = (min(cash, number(plan.order_amount)) / (price * (1 + fee_rate))).to_integral_value(rounding=ROUND_DOWN)
                else:
                    raise ValueError("Missing paper quantity/budget")
                if qty <= 0 or qty != qty.to_integral_value():
                    raise ValueError("Insufficient budget or invalid whole-share quantity")
                gross = number(money(qty * price))
                fee = number(money(gross * fee_rate))
                tax = number(money(gross * tax_rate)) if plan.action == "SELL" else Decimal(0)
                realized = Decimal(0)
                if plan.action == "BUY":
                    if gross + fee > cash:
                        raise ValueError("Insufficient paper cash")
                    new_cash, new_qty, new_cost = cash - gross - fee, held + qty, cost + gross + fee
                else:
                    if qty > held:
                        raise ValueError("Insufficient paper holdings")
                    removed_cost = cost if qty == held else number(money(cost * qty / held))
                    realized = gross - fee - tax - removed_cost
                    new_cash, new_qty, new_cost = cash + gross - fee - tax, held - qty, cost - removed_cost
                self._store_quotes(db, {plan.symbol: quote})
                self._snapshot(db, now)
                db.execute("UPDATE account SET cash=?,realized=? WHERE id=1", (money(new_cash), money(number(account["realized"]) + realized)))
                db.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?)", (plan.symbol, str(new_qty), money(new_cost)))
                result.update({"status": "SIMULATED_FILLED", "quantity": str(qty), "price": str(price),
                    "fee": money(fee), "tax": money(tax), "realizedPnl": money(realized),
                    "cashAfter": money(new_cash), "quoteTime": quote["timestamp"], "priceSource": quote.get("source")})
                self._snapshot(db, now)
            except (ValueError, KeyError, TypeError) as exc:
                result["reason"] = str(exc)
            db.execute("INSERT INTO fills VALUES(?,?,?)", (execution_id, now.isoformat(), json.dumps(result)))
            return result

    def returns(self, unit="day", through=None):
        through = date.fromisoformat(through) if through else now_kst().date()
        period_start(through, unit)
        with self.connect() as db:
            points = [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM valuations ORDER BY recorded_at,id")]
        groups = {}
        previous = None
        for point in points:
            day = datetime.fromisoformat(point["asOf"]).astimezone(KST).date()
            if day > through:
                continue
            key = period_start(day, unit).isoformat()
            if key not in groups:
                baseline_day = datetime.fromisoformat((previous or point)["asOf"]).astimezone(KST).date()
                groups[key] = {"start": previous or point, "end": point,
                    "partial": previous is None or baseline_day < date.fromisoformat(key) - timedelta(days=1)}
            groups[key]["end"] = point
            previous = point
        result = []
        for key, group in sorted(groups.items(), reverse=True)[:60]:
            start, end = group["start"], group["end"]
            valid = start["equity"] is not None and end["equity"] is not None and number(start["equity"]) > 0
            pnl = number(end["equity"]) - number(start["equity"]) if valid else None
            result.append({"period": key, "startAt": start["asOf"], "endAt": end["asOf"],
                "startEquity": start["equity"], "endEquity": end["equity"],
                "pnl": money(pnl) if valid else None,
                "returnPct": str(pnl / number(start["equity"]) * 100) if valid else None,
                "realizedPnl": money(number(end["realizedPnl"]) - number(start["realizedPnl"])),
                "partial": group["partial"], "basis": "recorded-equity", "unit": unit})
        return result
