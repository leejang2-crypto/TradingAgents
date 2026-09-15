import asyncio
from datetime import datetime
from unittest.mock import Mock

import pytest

import main
from tradingagents.llm_clients.routing import LLM_DEFAULTS, describe_routing


class StopScheduler(BaseException):
    pass


@pytest.fixture
def clock(monkeypatch, tmp_path):
    class Clock(datetime):
        current = datetime(2026, 9, 10, 9, 0, tzinfo=main.KOREA_TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "datetime", Clock)
    return Clock


@pytest.mark.parametrize("scenario", ["auto-paper", "auto-live"])
def test_two_slots_run_once_each_and_restart_does_not_replay(clock, monkeypatch, scenario):
    args = main.make_parser().parse_args(["--scenario", scenario, "--yes"])
    calls = []
    async def run(args, *, execute):
        calls.append((args.date, args._schedule_slot, execute))
        assert main.has_scheduler_completed(args.date, scenario, args._schedule_slot)
        return {"ok": True, "summaryPath": args._schedule_slot + ".json"}

    async def advance(_):
        if clock.current.hour == 9 and clock.current.minute == 0:
            assert main.build_monitor_snapshot()["runtime"]["nextRunAfter"] == "2026-09-10T09:20:00+09:00"
            clock.current = clock.current.replace(minute=20)
        elif clock.current.hour == 9:
            assert main.build_monitor_snapshot()["runtime"]["nextRunAfter"] == "2026-09-10T13:00:00+09:00"
            clock.current = clock.current.replace(hour=13, minute=0)
        else:
            assert main.build_monitor_snapshot()["runtime"]["nextRunAfter"] == "2026-09-11T09:00:00+09:00"
            raise StopScheduler

    monkeypatch.setattr(main, "run_auto_trading", run)
    monkeypatch.setattr(main.asyncio, "sleep", advance)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    assert calls == [("2026-09-10", slot, scenario == "auto-live") for slot in ("morning", "afternoon")]
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    assert len(calls) == 2
    slots = main.load_scheduler_state(scenario)["runs"]["2026-09-10"]["slots"]
    assert slots["morning"]["summaryPath"] == "morning.json"
    assert slots["afternoon"]["summaryPath"] == "afternoon.json"


@pytest.mark.parametrize("scenario", ["auto-paper", "auto-live"])
def test_late_start_runs_afternoon_only(clock, monkeypatch, scenario):
    clock.current = clock.current.replace(hour=14)
    args = main.make_parser().parse_args(["--scenario", scenario, "--yes"])
    calls = []
    async def run(args, *, execute):
        calls.append(args._schedule_slot)
        return {"ok": True}

    async def stop(_):
        raise StopScheduler

    monkeypatch.setattr(main, "run_auto_trading", run)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    assert calls == ["afternoon"]


@pytest.mark.parametrize("scenario", ["auto-paper", "auto-live"])
def test_interrupted_slot_is_reserved_but_next_slot_available(clock, scenario):
    main.mark_run_started("2026-09-10", scenario, "morning")
    assert main.has_scheduler_completed("2026-09-10", scenario, "morning")
    assert not main.has_scheduler_completed("2026-09-10", scenario, "afternoon")
    with pytest.raises(RuntimeError, match="already started"):
        main.mark_run_started("2026-09-10", scenario, "morning")


@pytest.mark.parametrize("scenario", ["auto-paper", "auto-live"])
def test_afternoon_live_once_uses_independent_slot(clock, monkeypatch, scenario):
    # Completion in one mode must never reserve the other mode.
    main.mark_scheduler_completed("2026-09-10", "morning.json", scenario=scenario, slot="morning")
    assert not main.has_scheduler_completed("2026-09-10", scenario, "afternoon")
    clock.current = clock.current.replace(hour=13)
    args = main.make_parser().parse_args(["--scenario", "auto-live", "--yes", "--run-mode", "once"])
    async def run(args, *, execute):
        return {"ok": True, "summaryPath": "afternoon.json"}

    monkeypatch.setattr(main, "run_auto_trading", run)
    asyncio.run(main.run_auto_live(args))
    with pytest.raises(RuntimeError, match="already started"):
        asyncio.run(main.run_auto_live(args))


@pytest.mark.parametrize("stamp,expected", [
    ("2026-09-10T09:45:00+09:00", {"morning"}),
    ("2026-09-10T14:00:00+09:00", {"afternoon"}),
    ("2026-09-10T04:00:00+00:00", {"afternoon"}),
    (None, {"morning", "afternoon"}),
    ("unknown", {"morning", "afternoon"}),
])
def test_legacy_migration_preserves_execution_history(clock, stamp, expected):
    old = {"status": "completed", "summaryPath": "old.json"}
    if stamp is not None:
        old["completedAt"] = stamp
    main.atomic_write_json(main.scheduler_state_path(), {"completedDates": ["2026-09-10"], "runs": {"2026-09-10": old}})
    state = main.load_scheduler_state()
    assert set(state["runs"]["2026-09-10"]["slots"]) == expected
    for slot in expected:
        assert state["runs"]["2026-09-10"]["slots"][slot]["summaryPath"] == "old.json"


def test_legacy_completed_dates_without_details_reserve_both(clock):
    main.atomic_write_json(main.scheduler_state_path(), {"completedDates": ["2026-09-10"]})
    assert main.has_scheduler_completed("2026-09-10", slot="morning")
    assert main.has_scheduler_completed("2026-09-10", slot="afternoon")


@pytest.mark.parametrize("hour,minute,expected", [(12, 59, "morning"), (13, 0, "afternoon"), (15, 0, "afternoon")])
def test_slot_boundary(clock, hour, minute, expected):
    assert main.active_trading_slot(clock.current.replace(hour=hour, minute=minute)) == expected


def test_next_day_is_open_not_next_day_afternoon(clock):
    args = main.make_parser().parse_args([])
    friday = datetime(2026, 9, 11, 14, 0, tzinfo=main.KOREA_TZ)
    result = main.next_trading_slot_time(friday, args, main.holiday_dates("2026-09-14"))
    assert result.isoformat() == "2026-09-15T09:00:00+09:00"


@pytest.mark.parametrize("current,close", [
    (datetime(2026, 9, 10, 16, tzinfo=main.KOREA_TZ), "15:30"),
    (datetime(2026, 9, 12, 13, tzinfo=main.KOREA_TZ), "15:30"),
    (datetime(2026, 9, 10, 13, tzinfo=main.KOREA_TZ), "12:30"),
])
def test_scheduler_does_not_trade_outside_market(clock, monkeypatch, current, close):
    clock.current = current
    args = main.make_parser().parse_args(["--market-close", close])
    runner = Mock(side_effect=AssertionError("no trading"))
    monkeypatch.setattr(main, "run_auto_trading", runner)
    async def stop(_):
        raise StopScheduler
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    runner.assert_not_called()


def test_default_is_hybrid_but_cloud_override_remains(clock):
    args = main.make_parser().parse_args([])
    assert args.provider == LLM_DEFAULTS["provider"] == "hybrid"
    main.initialize_trading_settings(args)
    routes = describe_routing(main.make_config(args, korea_mode=True, minimal_mode=False))
    assert routes["news_analyst"]["provider"] == "ollama"
    assert routes["portfolio_manager"]["provider"] == "openai"
    args = main.make_parser().parse_args(["--provider", "openai"])
    args._explicit_settings = {"provider"}
    main.initialize_trading_settings(args)
    assert args.provider == "openai"


def test_afternoon_news_snapshot_does_not_overwrite_morning(clock, monkeypatch):
    args = main.make_parser().parse_args(["--date", "2026-09-10"])
    monkeypatch.setattr(main, "collect_news_naver", lambda *a, **kw: [])
    morning = main.collect_korea_news_snapshot(args)
    clock.current = clock.current.replace(hour=13)
    afternoon = main.collect_korea_news_snapshot(args)
    assert morning["path"] != afternoon["path"]
    assert len(list((main.LOCAL_DIR / "news").glob("*.json"))) == 2


def test_slow_morning_run_finishes_before_afternoon_starts(clock, monkeypatch):
    clock.current = clock.current.replace(minute=20)
    args = main.make_parser().parse_args([])
    calls = []
    async def run(args, *, execute):
        calls.append(args._schedule_slot)
        if args._schedule_slot == "morning":
            clock.current = clock.current.replace(hour=13, minute=10)
            raise RuntimeError("morning analysis failed")
        return {"ok": True}

    async def stop(_):
        raise StopScheduler

    monkeypatch.setattr(main, "run_auto_trading", run)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    assert calls == ["morning", "afternoon"]
    slots = main.load_scheduler_state()["runs"]["2026-09-10"]["slots"]
    assert slots["morning"]["status"] == "completed-with-errors"
    assert slots["afternoon"]["status"] == "completed"
