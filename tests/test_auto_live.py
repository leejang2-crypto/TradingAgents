import asyncio
import json
from unittest.mock import Mock

import pytest

import main
from tradingagents.brokers import toss_executor
from tests.test_long_term_research import universe_snapshot


@pytest.fixture
def live_args(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    return main.make_parser().parse_args(["--scenario", "auto-live", "--yes", "--run-mode", "once"])


def test_live_and_paper_share_candidates_objective_and_analysis(live_args, monkeypatch):
    candidates = [{"ticker": "000660.KS"}, {"ticker": "035420.KS"}]
    universe = universe_snapshot()
    universe["members"].pop("000198.KS")
    universe["members"]["035420.KS"] = "NAVER"
    monkeypatch.setattr(main, "collect_candidate_snapshot", lambda args: {"ok": True, "universe": universe, "candidates": candidates})
    calls = []

    async def analyze(args, *, execute):
        calls.append((args.korea_ticker, execute, str(main.order_budget(args))))
        return {"ok": True, "ticker": args.korea_ticker, "decision": "Buy", "toss": {"dryRun": not execute}}

    monkeypatch.setattr(main, "run_korea_toss", analyze)
    paper_args = main.make_parser().parse_args(["--run-mode", "once"])
    paper = asyncio.run(main.run_auto_paper(paper_args))
    live = asyncio.run(main.run_auto_live(live_args))
    assert paper["selectedTickers"] == live["selectedTickers"] == ["000660.KS", "035420.KS"]
    assert paper["tradingObjective"] == live["tradingObjective"]
    assert calls == [(ticker, execute, "1000000") for execute in (False, True) for ticker in ("000660.KS", "035420.KS")]
    assert "/auto_paper/" in paper["summaryPath"]
    assert "/auto_live/" in live["summaryPath"]
    assert live["executionMode"] == "live"
    assert live_args.korea_ticker == "005930.KS"


def test_live_requires_authorization_before_pipeline(live_args, monkeypatch):
    live_args.yes = False
    collect = Mock(side_effect=AssertionError("must not collect"))
    monkeypatch.setattr(main, "collect_candidate_snapshot", collect)
    with pytest.raises(SystemExit, match="--yes"):
        asyncio.run(main.run_auto_live(live_args))
    collect.assert_not_called()


def test_live_once_rejects_closed_market_before_reserving_date(live_args, monkeypatch):
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: False)
    with pytest.raises(ValueError, match="regular session"):
        asyncio.run(main.run_auto_live(live_args))
    assert not main.scheduler_state_path("auto-live").exists()


def test_live_rejects_historical_analysis_date(live_args):
    live_args.date = "2000-01-01"
    with pytest.raises(ValueError, match="today"):
        asyncio.run(main.run_auto_live(live_args))


def test_paper_completion_does_not_skip_live_and_live_is_not_replayed(live_args, monkeypatch):
    run_date = main.today_korea().isoformat()
    main.mark_scheduler_completed(run_date, "paper.json")
    calls = []

    async def run(args, *, execute):
        assert main.load_scheduler_state("auto-live")["runs"][run_date]["status"] == "started"
        calls.append(execute)
        return {"ok": True, "summaryPath": "live.json"}

    monkeypatch.setattr(main, "run_auto_trading", run)
    asyncio.run(main.run_auto_live(live_args))
    with pytest.raises(RuntimeError, match="already started"):
        asyncio.run(main.run_auto_live(live_args))
    assert calls == [True]
    assert main.load_scheduler_state()["runs"][run_date]["summaryPath"] == "paper.json"
    assert main.load_scheduler_state("auto-live")["runs"][run_date]["summaryPath"] == "live.json"


def test_interrupted_live_run_remains_reserved(live_args, monkeypatch):
    async def interrupt(args, *, execute):
        raise RuntimeError("lost response")

    monkeypatch.setattr(main, "run_auto_trading", interrupt)
    with pytest.raises(RuntimeError, match="lost response"):
        asyncio.run(main.run_auto_live(live_args))
    with pytest.raises(RuntimeError, match="already started"):
        asyncio.run(main.run_auto_live(live_args))
    assert main.load_scheduler_state("auto-live")["runs"][live_args.date]["status"] == "started"


def test_live_rechecks_session_after_analysis(live_args, monkeypatch):
    live_args.date = main.today_korea().isoformat()
    graph = Mock()
    graph.propagate.return_value = ({"final_trade_decision": "**Rating**: Buy"}, "Buy")
    graph.save_reports.return_value = "report.md"
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: False)
    executor = Mock(side_effect=AssertionError("must not send order"))
    monkeypatch.setattr(main, "execute_plan", executor)
    result = asyncio.run(main.run_korea_toss(live_args, execute=True))
    assert result["toss"]["skipped"] == "market-closed"
    assert result["toss"]["dryRun"] is False
    executor.assert_not_called()


def test_daily_live_scheduler_uses_shared_loop(live_args, monkeypatch):
    class MarketTime(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 17, 10, tzinfo=main.KOREA_TZ).astimezone(tz)

    monkeypatch.setattr(main, "datetime", MarketTime)
    class StopLoop(Exception):
        pass

    calls = []

    async def run(args):
        calls.append(args.date)
        return {"ok": True, "summaryPath": "live.json"}

    async def stop(_):
        raise StopLoop

    monkeypatch.setattr(main, "run_auto_live", run)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(StopLoop):
        asyncio.run(main.run_daily_market_scheduler(live_args))
    assert len(calls) == 1
    assert main.has_scheduler_completed(calls[0], "auto-live")
    assert not main.has_scheduler_completed(calls[0], "auto-paper")
    assert main.build_monitor_snapshot()["executionMode"] == "live"


def test_live_and_paper_runners_cannot_overlap(live_args):
    paper_args = main.make_parser().parse_args([])
    with main.scheduler_process_lock(paper_args):
        with pytest.raises(RuntimeError, match="already running"):
            with main.scheduler_process_lock(live_args):
                pass


def test_monitor_selects_live_artifacts(live_args):
    main.atomic_write_json(main.LOCAL_DIR / "auto_paper" / "auto_paper_test.json", {"scenario": "auto-paper"})
    main.atomic_write_json(main.LOCAL_DIR / "auto_live" / "auto_live_test.json", {"scenario": "auto-live"})
    main.write_monitor_status("waiting-for-market-open", executionMode="live")
    snapshot = main.build_monitor_snapshot()
    assert snapshot["latestSummary"]["scenario"] == "auto-live"
    rendered = main.render_monitor_html(snapshot)
    assert "실투자 · Toss 실제 주문" in rendered
    assert "Recent Live Orders" in rendered


@pytest.mark.parametrize("failure", [False, True])
def test_live_executor_audits_request_and_response_without_paper_trade(tmp_path, monkeypatch, failure):
    for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"):
        monkeypatch.setenv(key, "test-credential")
    submitted = []
    log_path = tmp_path / "live.jsonl"

    class FakeToss:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create_order(self, payload):
            assert json.loads(log_path.read_text().splitlines()[0])["status"] == "SUBMITTING"
            submitted.append(payload)
            if failure:
                raise TimeoutError("response lost")
            return {"orderId": "fake-order"}

    monkeypatch.setattr(toss_executor, "TossClient", FakeToss)
    plan = toss_executor.plan_from_decision("**Rating**: Buy", "000660.KS")
    call = toss_executor.execute_plan(plan, execute=True, live_order_log=log_path, paper_trade_log=tmp_path / "paper.jsonl", experience_run_id="live-test-run")
    if failure:
        with pytest.raises(TimeoutError):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result["executed"] and not result["dryRun"]
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(submitted) == 1
    assert [row["status"] for row in records] == ["SUBMITTING", "UNCONFIRMED" if failure else "SUBMITTED"]
    assert all(row["payload"]["clientOrderId"] == submitted[0]["clientOrderId"] for row in records)
    assert all(row["experienceRunId"] == "live-test-run" for row in records)
    assert not (tmp_path / "paper.jsonl").exists()
    assert "test-credential" not in log_path.read_text()


def test_live_hold_never_contacts_broker(monkeypatch):
    client = Mock(side_effect=AssertionError("HOLD must not contact broker"))
    monkeypatch.setattr(toss_executor, "TossClient", client)
    result = asyncio.run(toss_executor.execute_plan(toss_executor.plan_from_decision("**Rating**: Hold", "005930.KS"), execute=True))
    assert not result["executed"] and not result["dryRun"]
    client.assert_not_called()
