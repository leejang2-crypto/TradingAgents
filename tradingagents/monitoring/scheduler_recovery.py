"""Conservative, read-only eligibility checks for the old premature-slot bug."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


OLD_TIME_ERROR = "시장 기준자료 확인 실패 (ValueError): 정규장 신규 매수 가능 시간 밖"
KST = ZoneInfo("Asia/Seoul")


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Ambiguous timestamp")
    return parsed.astimezone(KST)


def _no_orders(local, day):
    # Both ledgers must be readable. Never create/reset an account to prove absence.
    risk_path = local / "positions" / "paper.sqlite3"
    account_path = local / "accounts" / "paper.sqlite3"
    with closing(sqlite3.connect(risk_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        state = json.loads(db.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])
    for order in state["orders"].values():
        if order["status"] in {"SUBMITTING", "UNCONFIRMED", "PENDING"}:
            return False
        if _stamp(order["createdAt"]).date() == day:
            return False
    with closing(sqlite3.connect(account_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        stamps = db.execute("SELECT recorded_at FROM fills").fetchall()
    return not any(_stamp(row[0]).date() == day for row in stamps)


def _no_later_trade(local, start, end):
    # An explicit rerun could reuse the bad slot without updating its old record.
    lines = (local / "monitor" / "manual_runs.jsonl").read_text(encoding="utf-8").splitlines()
    matched = False
    for line in lines:
        event = json.loads(line)
        if event["event"] == "started" and event["job"]["action"] == "trade":
            at = _stamp(event["at"])
            if at.date() == end.date() and at > end:
                return False
            matched = matched or start <= at <= end
    return matched


def recoverable_paper_slots(local, state, run_date):
    """Return only today's provably order-free, pre-entry failures; no writes."""
    local = Path(local)
    records = state.get("runs", {}).get(run_date, {}).get("slots", {})
    eligible = []
    for slot, record in records.items():
        try:
            if record.get("status") != "completed-with-errors":
                continue
            path = Path(record["summaryPath"]).resolve()
            if path.parent != (local / "auto_paper").resolve() or not path.name.startswith("auto_paper_"):
                continue
            summary = json.loads(path.read_text(encoding="utf-8"))
            if not (summary.get("scenario") == "auto-paper" and summary.get("executionMode") == "dry-run"
                    and summary.get("date") == run_date and summary.get("scheduleSlot") == slot
                    and summary.get("ok") is False and summary.get("analyses") == []
                    and summary.get("selectedTickers") == [] and summary.get("selectionReason") == OLD_TIME_ERROR):
                continue
            start, end = _stamp(record["startedAt"]), _stamp(record["completedAt"])
            entry = datetime.combine(start.date(), datetime.strptime(summary["tradingObjective"]["buy_start"], "%H:%M").time(), KST)
            if not (start.date().isoformat() == run_date == end.date().isoformat()
                    and start <= end < entry and slot == entry.strftime("scan-%H%M")):
                continue
            if _no_orders(local, start.date()) and _no_later_trade(local, start, end):
                eligible.append(slot)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
            # Missing/corrupt evidence must keep the reservation, never replay it.
            continue
    return eligible
