import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

import main
from tests.test_dashboard_auth import server as server, request, login
from tests.test_manual_control import args as args, start_manual
from tests.test_twice_daily_schedule import clock as clock
from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.monitoring.control import RunController, scheduled_job


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("hour,minute,status", [(9, 35, 200), (9, 59, 200), (10, 0, 202),
                                                (14, 29, 202), (14, 30, 200)])
def test_trade_request_checks_buy_window_before_queue(clock, server, live, hour, minute, status):
    clock.current = clock.current.replace(hour=hour, minute=minute)
    server.control = RunController(live=live)
    payload = {"action": "trade", "requestId": "b" * 32, "confirmLive": live}
    code, _, body = request(server, "POST", "/api/control/run", payload, cookie=login(server))
    result = json.loads(body)
    assert code == status
    assert not main.scheduler_state_path().exists()
    if status == 200:
        assert result["ok"] and result["skipped"] == "entry-window"
        assert server.control.pending is None
        assert "10:00" in result["notice"] if hour < 10 else "14:30" in result["notice"]
    else:
        assert server.control.pending["action"] == "trade"


def test_early_trade_does_not_stop_current_job(clock, server):
    clock.current = clock.current.replace(hour=9, minute=35)
    server.control.start(scheduled_job())
    before = server.control.snapshot()
    code, _, _ = request(server, "POST", "/api/control/run", {
        "action": "trade", "requestId": "b" * 32, "policy": "stop", "activeId": before["active"]["id"],
    }, cookie=login(server))
    assert code == 200 and not server.control.cancel.is_set()
    assert server.control.snapshot() == before


@pytest.mark.parametrize("calendar", ["delayed", "closed", "failed"])
def test_trade_uses_exchange_calendar_and_fails_closed(clock, server, calendar):
    clock.current = clock.current.replace(hour=10)
    server.risk = Mock(spec=PositionRiskManager)
    if calendar == "failed":
        server.risk._buy_window.side_effect = ValueError("calendar unavailable")
    else:
        server.risk._buy_window.return_value = None if calendar == "closed" else (
            clock.current.replace(hour=11), clock.current.replace(hour=14, minute=30))
    code, _, body = request(server, "POST", "/api/control/run", {
        "action": "trade", "requestId": "b" * 32,
    }, cookie=login(server))
    assert code == (503 if calendar == "failed" else 200)
    server.risk._buy_window.assert_called_once()
    assert server.control.pending is None
    if calendar == "delayed":
        assert "11:00" in json.loads(body)["notice"]


@pytest.mark.parametrize("hour,minute", [(9, 35), (14, 30)])
@pytest.mark.parametrize("live", [False, True])
def test_worker_rechecks_entry_window_without_reserving(clock, args, monkeypatch, hour, minute, live):
    clock.current = clock.current.replace(hour=10)
    args.scenario = "auto-live" if live else "auto-paper"
    control = RunController(live=live)
    job = start_manual(control, args, action="trade", confirmLive=live)
    clock.current = clock.current.replace(hour=hour, minute=minute)
    trading, notify = AsyncMock(), Mock()
    monkeypatch.setattr(main, "run_auto_trading", trading)
    monkeypatch.setattr(main, "notify", notify)
    asyncio.run(main.run_manual_job(args, control, job))
    assert control.last["status"] == "skipped" and control.last["result"]["ok"]
    assert not main.scheduler_state_path(args.scenario).exists()
    trading.assert_not_called()
    notify.assert_called_once()
    assert notify.call_args.args[1] == "analysis"


def test_time_veto_before_entry_does_not_consume_first_scan(clock, args, monkeypatch):
    args.candidate_scan_minutes = 30
    clock.current = clock.current.replace(hour=9, minute=35)
    control = RunController()
    trading = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "run_auto_trading", trading)
    asyncio.run(main.run_manual_job(args, control, start_manual(control, args, action="trade")))
    assert not main.has_scheduler_completed(args.date, "auto-paper", "scan-1000")
    clock.current = clock.current.replace(hour=10, minute=0)
    asyncio.run(main.run_manual_job(args, control, start_manual(control, args, n=2, action="trade")))
    assert control.last["status"] == "completed"
    assert main.has_scheduler_completed(args.date, "auto-paper", "scan-1000")
    trading.assert_awaited_once()
