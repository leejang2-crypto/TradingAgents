"""Read-only market/account service; no create/cancel/modify order operations."""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .paper_account import PaperAccount, now_kst, period_start
from .toss_client import TossClient

_refresh_lock = threading.Lock()
_last_refresh = {}


def error_label(exc):
    status = getattr(exc, "status_code", None)
    return type(exc).__name__ + (f" ({status})" if status is not None else "")


def credentials():
    client_id, secret = os.getenv("TOSS_CLIENT_ID"), os.getenv("TOSS_CLIENT_SECRET")
    if not client_id or not secret:
        raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 설정이 필요합니다.")
    return client_id, secret


async def fetch_quotes(symbols):
    if not symbols:
        return {}
    client_id, secret = credentials()
    result = {}
    async with TossClient(client_id, secret) as client:
        for offset in range(0, len(symbols), 200):
            rows = await client.get_prices(symbols[offset:offset + 200])
            if not isinstance(rows, list):
                raise ValueError("Invalid Toss price response")
            for row in rows:
                if isinstance(row, dict) and row.get("symbol") in symbols:
                    result[row["symbol"]] = {**row, "source": "Toss prices"}
    return result


def paper_account(local_dir, capital="10000000"):
    account = PaperAccount(Path(local_dir) / "accounts" / "paper.sqlite3")
    account.initialize(capital, fee_rate=os.getenv("TRADINGAGENTS_PAPER_FEE_RATE", "0"),
        tax_rate=os.getenv("TRADINGAGENTS_PAPER_SELL_TAX_RATE", "0"),
        slippage=os.getenv("TRADINGAGENTS_PAPER_SLIPPAGE_RATE", "0"))
    return account


def paper_history(local_dir, journal, limit=30):
    """Render ledger-backed executions separately from legacy order intents."""
    path = Path(local_dir) / "accounts" / "paper.sqlite3"
    fills = {}
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for row in db.execute("SELECT payload FROM fills ORDER BY recorded_at DESC,rowid DESC LIMIT ?", (limit,)):
                fill = json.loads(row[0])
                fills[fill["id"]] = fill
            for entry in journal:
                key = (entry.get("simulation") or {}).get("id") or (entry.get("payload") or {}).get("clientOrderId")
                if key and key not in fills:
                    row = db.execute("SELECT payload FROM fills WHERE id=?", (key,)).fetchone()
                    if row:
                        fills[key] = json.loads(row[0])
        finally:
            db.close()
    rows, seen = [], set()
    for entry in journal:
        key = (entry.get("simulation") or {}).get("id") or (entry.get("payload") or {}).get("clientOrderId")
        if key in fills:
            if key in seen:
                continue
            seen.add(key)
            fill = fills[key]
            rows.append({**entry, "recordedAt": fill["recordedAt"], "status": fill["status"],
                         "simulation": fill, "ledgerBacked": True, "fillVerified": False})
        else:
            rows.append({**entry, "status": "RECORDED", "originalStatus": entry.get("status"),
                         "simulation": None, "ledgerBacked": False, "fillVerified": False})
    for key, fill in fills.items():
        if key not in seen:
            rows.append({"recordedAt": fill["recordedAt"], "status": fill["status"],
                         "plan": {"symbol": fill["symbol"], "action": fill["side"]},
                         "simulation": fill, "ledgerBacked": True, "fillVerified": False})
    return sorted(rows, key=lambda row: row.get("recordedAt", ""))[-limit:]


def live_cache_path(local_dir):
    # Never mix account snapshots when the configured account changes.
    key = hashlib.sha256((os.getenv("TOSS_CLIENT_ID", "") + ":" + os.getenv("TOSS_ACCOUNT_SEQ", "")).encode()).hexdigest()[:24]
    return Path(local_dir) / "accounts" / f"live_{key}.sqlite3"


def _live_db(local_dir):
    import sqlite3
    path = live_cache_path(local_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)")
    return db


async def fetch_live_snapshot():
    client_id, secret = credentials()
    account_seq = os.getenv("TOSS_ACCOUNT_SEQ")
    if not account_seq:
        raise ValueError("TOSS_ACCOUNT_SEQ 설정이 필요합니다.")
    result = {"mode": "live", "asOf": now_kst().isoformat(), "errors": {},
        "holdings": None, "buyingPower": {}, "orders": []}
    async with TossClient(client_id, secret, account_seq=account_seq) as client:
        try:
            holdings = await client.get_holdings()
            if not isinstance(holdings, dict) or not isinstance(holdings.get("items"), list):
                raise ValueError("Invalid holdings response")
            result["holdings"] = holdings
        except Exception as exc:
            result["errors"]["holdings"] = error_label(exc)
        for currency in ("KRW", "USD"):
            try:
                result["buyingPower"][currency] = await client.get_buying_power(currency)
            except Exception as exc:
                result["errors"]["buyingPower" + currency] = error_label(exc)
        # Recent executions only; no claim that this is complete lifetime history.
        since = (now_kst().date() - timedelta(days=30)).isoformat()
        for group in ("OPEN", "CLOSED"):
            try:
                page = await client.get_orders(status=group, from_date=since, to_date=now_kst().date().isoformat())
                if not isinstance(page, dict) or not isinstance(page.get("orders"), list):
                    raise ValueError("Invalid order response")
                result["orders"].extend(page["orders"])
                if page.get("hasNext"):
                    result["ordersTruncated"] = True
            except Exception as exc:
                result["errors"]["orders" + group] = error_label(exc)
            await asyncio.sleep(0.25)
    result["orders"] = list({r["orderId"]: r for r in result["orders"] if isinstance(r, dict) and "orderId" in r}.values())
    return result


def refresh_account(local_dir, mode, capital="10000000"):
    """Serialized and rate-limited across browser tabs. Called off the main loop."""
    if mode not in {"dry-run", "live"}:
        raise ValueError("Invalid account mode")
    key = (str(Path(local_dir).resolve()), mode, str(live_cache_path(local_dir)) if mode == "live" else "")
    with _refresh_lock:
        if time.monotonic() - _last_refresh.get(key, -1000) < 60:
            return
        _last_refresh[key] = time.monotonic()
        if mode == "dry-run":
            account = paper_account(local_dir, capital)
            quotes = asyncio.run(fetch_quotes(account.symbols()))
            account.refresh(quotes)
        else:
            payload = asyncio.run(fetch_live_snapshot())
            db = _live_db(local_dir)
            try:
                with db:
                    db.execute("INSERT INTO snapshots(recorded_at,payload) VALUES(?,?)", (payload["asOf"], json.dumps(payload)))
            finally:
                db.close()


def account_view(local_dir, mode, unit="day", through=None, capital="10000000"):
    through = date.fromisoformat(through) if through else now_kst().date()
    period_start(through, unit)
    if mode == "dry-run":
        account = paper_account(local_dir, capital)
        return {"ok": True, "mode": mode, "account": account.view(),
                "returns": account.returns(unit, through.isoformat()), "unit": unit}
    if mode != "live":
        raise ValueError("Invalid account mode")
    db = _live_db(local_dir)
    try:
        row = db.execute("SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        db.close()
    snapshot = json.loads(row[0]) if row else None
    # Buying power is not a cash balance. Holdings returns exclude closed positions
    # and cannot be compounded into an account's week/month/year performance.
    return {"ok": True, "mode": mode, "account": snapshot, "unit": unit, "returns": [],
        "returnAvailability": "계좌 기간 수익률 API 미지원: 입출금·전체 잔고 이력 없이 계산하지 않습니다.",
        "dailyHoldingsReturn": (snapshot or {}).get("holdings", {}).get("dailyProfitLoss") if (snapshot or {}).get("holdings") else None}
