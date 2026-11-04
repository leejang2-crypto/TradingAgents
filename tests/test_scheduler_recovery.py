import json
import sqlite3
from copy import deepcopy

import pytest

import main
from tests.test_twice_daily_schedule import clock as clock
from tradingagents.brokers.paper_account import PaperAccount
from tradingagents.brokers.position_risk import RiskStore
from tradingagents.monitoring.scheduler_recovery import OLD_TIME_ERROR, recoverable_paper_slots


@pytest.fixture
def incident(clock, tmp_path):
    clock.current = clock.current.replace(hour=9, minute=35)
    store = RiskStore(tmp_path / "positions/paper.sqlite3")
    store.save(store.load())
    PaperAccount(tmp_path / "accounts/paper.sqlite3")
    day = clock.current.date().isoformat()
    summary = {"ok": False, "scenario": "auto-paper", "executionMode": "dry-run", "date": day,
               "scheduleSlot": "scan-1000", "analyses": [], "selectedTickers": [],
               "selectionReason": OLD_TIME_ERROR, "tradingObjective": {"buy_start": "10:00"}}
    path = tmp_path / "auto_paper/auto_paper_incident.json"
    main.atomic_write_json(path, summary)
    main.mark_run_started(day, "auto-paper", "scan-1000")
    clock.current = clock.current.replace(second=4)
    main.mark_scheduler_completed(day, str(path), "completed-with-errors", slot="scan-1000")
    audit = tmp_path / "monitor/manual_runs.jsonl"
    audit.parent.mkdir()
    audit.write_text(json.dumps({"event": "started", "at": day + "T09:35:01+09:00",
                                 "job": {"action": "trade"}}) + "\n")
    return day, summary, path


def test_read_only_preview_then_backed_up_idempotent_recovery(incident, tmp_path):
    day, _, _ = incident
    state = main.load_scheduler_state()
    before = deepcopy(state)
    assert recoverable_paper_slots(tmp_path, state, day) == ["scan-1000"]
    assert state == before and main.load_scheduler_state() == before
    args = main.make_parser().parse_args([])
    with main.scheduler_process_lock(args):
        assert main.recover_premature_paper_slots(args) == ["scan-1000"]
        assert main.recover_premature_paper_slots(args) == []
    assert not main.has_scheduler_completed(day, slot="scan-1000")
    backups = list((tmp_path / "scheduler/recovery").glob("*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text())["state"] == before
    assert main.load_scheduler_state()["completedDates"] == []


@pytest.mark.parametrize("changes", [
    {"executionMode": "live"}, {"scenario": "auto-live"}, {"ok": True},
    {"analyses": [{"ticker": "005930.KS"}]}, {"selectedTickers": ["005930.KS"]},
    {"selectionReason": "API error"}, {"scheduleSlot": "scan-1030"}, {"date": "2000-01-01"},
])
def test_other_failures_or_possible_trades_are_never_recovered(incident, tmp_path, changes):
    day, summary, path = incident
    main.atomic_write_json(path, {**summary, **changes})
    assert recoverable_paper_slots(tmp_path, main.load_scheduler_state(), day) == []


@pytest.mark.parametrize("change", ["started", "completed", "after-entry", "missing", "live-path"])
def test_unknown_ongoing_or_valid_reservations_are_preserved(incident, tmp_path, change):
    day, _, path = incident
    state = main.load_scheduler_state()
    record = state["runs"][day]["slots"]["scan-1000"]
    if change in {"started", "completed"}:
        record["status"] = change
    elif change == "after-entry":
        record["completedAt"] = day + "T10:00:00+09:00"
    elif change == "missing":
        path.unlink()
    else:
        record["summaryPath"] = str(tmp_path / "auto_live/auto_paper_incident.json")
    assert recoverable_paper_slots(tmp_path, state, day) == []


@pytest.mark.parametrize("evidence", ["order", "fill", "pending", "missing-db", "bad-time", "later-order"])
def test_ledger_checks_fail_closed(incident, tmp_path, evidence):
    day, _, _ = incident
    if evidence == "fill":
        with sqlite3.connect(tmp_path / "accounts/paper.sqlite3") as db:
            db.execute("INSERT INTO fills VALUES (?, ?, ?)", ("test", day + "T09:35:02+09:00", "{}"))
    elif evidence == "missing-db":
        (tmp_path / "positions/paper.sqlite3").unlink()
    else:
        store = RiskStore(tmp_path / "positions/paper.sqlite3")
        state = store.load()
        state["orders"]["test"] = {"status": "PENDING" if evidence == "pending" else "FILLED",
                                   "createdAt": "unknown" if evidence == "bad-time" else day + "T09:35:02+09:00"}
        if evidence == "later-order":
            state["orders"]["test"]["createdAt"] = day + "T10:05:00+09:00"
        store.save(state)
    assert recoverable_paper_slots(tmp_path, main.load_scheduler_state(), day) == []


@pytest.mark.parametrize("evidence", ["later-run", "missing-audit", "corrupt-audit", "empty-audit"])
def test_explicit_rerun_or_missing_audit_keeps_reservation(incident, tmp_path, evidence):
    day, _, _ = incident
    audit = tmp_path / "monitor/manual_runs.jsonl"
    if evidence == "missing-audit":
        audit.unlink()
    elif evidence == "corrupt-audit":
        audit.write_text("corrupt")
    elif evidence == "empty-audit":
        audit.write_text("")
    else:
        audit.write_text(json.dumps({"event": "started", "at": day + "T10:05:00+09:00",
                                     "job": {"action": "trade"}}) + "\n")
    assert recoverable_paper_slots(tmp_path, main.load_scheduler_state(), day) == []


def test_live_launch_and_past_dates_are_not_recovered(incident, tmp_path):
    day, _, _ = incident
    args = main.make_parser().parse_args(["--scenario", "auto-live"])
    assert main.recover_premature_paper_slots(args) == []
    assert recoverable_paper_slots(tmp_path, main.load_scheduler_state(), "2026-09-11") == []
    assert main.has_scheduler_completed(day, slot="scan-1000")


def test_recovery_preserves_other_slots_and_backup_failure_leaves_state(incident, tmp_path, monkeypatch):
    day, _, _ = incident
    main.mark_scheduler_completed(day, "valid.json", slot="scan-1030")
    before = main.load_scheduler_state()
    args = main.make_parser().parse_args([])
    write = main.atomic_write_json
    monkeypatch.setattr(main, "atomic_write_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        main.recover_premature_paper_slots(args)
    assert main.load_scheduler_state() == before
    monkeypatch.setattr(main, "atomic_write_json", write)
    assert main.recover_premature_paper_slots(args) == ["scan-1000"]
    after = main.load_scheduler_state()["runs"][day]
    assert after["slots"] == {"scan-1030": before["runs"][day]["slots"]["scan-1030"]}
    assert after["lastSlot"] == "scan-1030"
