import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.runnables import RunnableLambda

import main
from tradingagents.monitoring.control import (
    BusyError, RunController, RunStopped, cancellation_callbacks, checkpoint, scheduled_job,
)
from tradingagents.brokers.toss_executor import TossOrderPlan, execute_plan
from tradingagents.brokers.toss_client import TossClient


def command(n=1, **extra):
    return {"requestId": str(n).zfill(32), "action": "analyze", "ticker": "005930.KS", **extra}


@pytest.mark.parametrize("policy", ["stop", "wait"])
def test_single_lane_busy_choice_and_settings_snapshot(policy):
    control = RunController()
    control.start(scheduled_job())
    active_id = control.active["id"]
    settings = {"provider": "hybrid"}
    with pytest.raises(BusyError):
        control.submit(command(), settings)
    with pytest.raises(BusyError):
        control.submit(command(policy=policy, activeId="stale"), settings)
    control.submit(command(policy=policy, activeId=active_id), settings)
    settings["provider"] = "openai"
    assert control.pending["settings"]["provider"] == "hybrid"
    assert control.cancel.is_set() == (policy == "stop")
    with pytest.raises(BusyError):
        control.start()
    control.finish("stopped" if policy == "stop" else "completed")
    job = control.start()
    assert job["id"] == command()["requestId"]
    assert not control.cancel.is_set()


def test_concurrent_requests_only_enqueue_one_job():
    control = RunController()
    def submit(n):
        try:
            control.submit(command(n), {})
            return True
        except BusyError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(8))) == 1


def test_idempotency_and_restart_never_replay(tmp_path):
    path = tmp_path / "audit.jsonl"
    control = RunController(audit_path=path)
    control.submit(command(), {})
    control.submit(command(), {})
    control.pause(True)
    restarted = RunController(audit_path=path)
    assert restarted.pending is None and restarted.active is None and restarted.paused
    assert restarted.last["status"] == "interrupted"
    restarted.submit(command(), {})
    assert restarted.pending is None
    with pytest.raises(ValueError, match="different command"):
        restarted.submit(command(action="news"), {})


def test_pause_does_not_cancel_active_but_manual_jobs_still_run():
    control = RunController()
    control.start(scheduled_job())
    control.pause(True)
    assert not control.cancel.is_set()
    control.finish("completed")
    assert control.start(scheduled_job()) is None
    control.submit(command(), {})
    assert control.start()["source"] == "manual"


def test_cancelling_pending_does_not_revoke_active_stop(tmp_path):
    control = RunController(audit_path=tmp_path / "audit.jsonl")
    control.start(scheduled_job())
    control.submit(command(policy="stop", activeId=control.active["id"]), {})
    with pytest.raises(BusyError):
        control.cancel_pending("stale-id")
    control.cancel_pending(command()["requestId"])
    assert control.pending is None and control.cancel.is_set()
    assert RunController(audit_path=tmp_path / "audit.jsonl").last["status"] == "cancelled"


def test_live_mode_requires_explicit_confirmation():
    control = RunController(live=True)
    with pytest.raises(ValueError, match="확인"):
        control.submit(command(action="trade"), {})
    control.submit(command(action="trade", confirmLive=True), {})
    assert control.snapshot()["mode"] == "live"


def test_cancellation_callback_prevents_next_chain():
    control = RunController()
    control.start(scheduled_job())
    calls = []
    def first(value):
        calls.append("first")
        control.cancel.set()
        return value
    chain = RunnableLambda(first) | RunnableLambda(lambda value: calls.append("second"))
    with control.scope(), pytest.raises(RunStopped):
        chain.invoke("input", config={"callbacks": cancellation_callbacks()})
    assert calls == ["first"]
    checkpoint()  # No leaked context after leaving the scope.


@pytest.fixture
def args(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    result = main.make_parser().parse_args([])
    result.date = main.today_korea().isoformat()
    main.initialize_trading_settings(result)
    return result


def start_manual(control, args, **extra):
    control.submit(command(**extra), main.default_settings(args))
    return control.start()


def test_manual_analysis_never_calls_order_executor(args, monkeypatch):
    graph = Mock()
    graph.propagate.return_value = ({"final_trade_decision": "**Rating**: Buy"}, "Buy")
    graph.save_reports.return_value = "report.md"
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    executor = AsyncMock()
    monkeypatch.setattr(main, "execute_plan", executor)
    control = RunController()
    asyncio.run(main.run_manual_job(args, control, start_manual(control, args)))
    assert control.last["status"] == "completed"
    assert control.last["result"]["toss"]["skipped"] == "analysis-only"
    executor.assert_not_called()


def test_stop_after_analysis_blocks_order(args, monkeypatch):
    control = RunController()
    control.start(scheduled_job())
    def analyze(*a):
        control.cancel.set()
        return {"final_trade_decision": "**Rating**: Buy"}, "Buy"
    graph = Mock()
    graph.propagate.side_effect = analyze
    graph.save_reports.return_value = "report.md"
    monkeypatch.setattr(main, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    executor = AsyncMock()
    monkeypatch.setattr(main, "execute_plan", executor)
    with control.scope(), pytest.raises(RunStopped):
        asyncio.run(main.run_korea_toss(args, execute=False))
    executor.assert_not_called()


def test_trade_rechecks_market_after_queueing(args, monkeypatch):
    control = RunController()
    job = start_manual(control, args, action="trade")
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: False)
    trading = AsyncMock()
    monkeypatch.setattr(main, "run_auto_trading", trading)
    asyncio.run(main.run_manual_job(args, control, job))
    assert control.last["status"] == "failed"
    trading.assert_not_called()


def test_daily_scheduler_consumes_manual_queue_while_paused(args, monkeypatch):
    control = RunController()
    control.pause(True)
    control.submit(command(), main.default_settings(args))
    args._controller = control
    analyze = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "run_korea_toss", analyze)
    class EndTest(Exception):
        pass
    async def stop_loop(*a):
        raise EndTest()
    monkeypatch.setattr(main, "scheduler_wait", stop_loop)
    with pytest.raises(EndTest):
        asyncio.run(main.run_daily_market_scheduler(args))
    analyze.assert_awaited_once()
    assert analyze.call_args.args[0]._analysis_only
    assert control.last["status"] == "completed"
    assert not main.has_scheduler_completed(args.date)


def test_manual_trade_reserves_current_slot(args, monkeypatch):
    control = RunController()
    job = start_manual(control, args, action="trade")
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    trading = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "run_auto_trading", trading)
    asyncio.run(main.run_manual_job(args, control, job))
    assert control.last["status"] == "completed"
    assert main.has_scheduler_completed(args.date, "auto-paper", main.active_trading_slot(main.datetime.now(main.KOREA_TZ)))
    assert trading.call_args.kwargs["execute"] is False


def test_unknown_live_order_blocks_replay(args):
    path = main.LOCAL_DIR / "live_orders/orders.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "UNCONFIRMED", "payload": {"clientOrderId": "test"}}) + "\n")
    with pytest.raises(ValueError, match="불확실"):
        main.require_resolved_live_orders()


def test_cancellation_after_quote_does_not_fill_paper(args, monkeypatch, tmp_path):
    control = RunController()
    control.start(scheduled_job())
    async def quotes(*a):
        control.cancel.set()
        return {}
    monkeypatch.setattr("tradingagents.brokers.account_service.fetch_quotes", quotes)
    fill = Mock()
    monkeypatch.setattr("tradingagents.brokers.paper_account.PaperAccount.fill", fill)
    with control.scope(), pytest.raises(RunStopped):
        asyncio.run(execute_plan(TossOrderPlan("BUY", "Buy", "005930", order_amount="100000"),
                                 paper_account_path=tmp_path / "paper.sqlite3"))
    fill.assert_not_called()


def test_stop_during_authentication_prevents_order_http_post():
    control = RunController()
    control.start(scheduled_job())
    async def run():
        async with TossClient("fake-id", "fake-secret", "fake-account") as client:
            async def headers(**kwargs):
                control.cancel.set()
                return {}
            client._headers = headers
            client._client.post = AsyncMock()
            with control.scope(), pytest.raises(RunStopped):
                await client.create_order({"symbol": "005930"})
            client._client.post.assert_not_called()
    asyncio.run(run())


def test_aborted_before_send_is_not_recorded_as_uncertain(args, monkeypatch):
    control = RunController()
    control.start(scheduled_job())
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"):
        monkeypatch.setenv(name, "synthetic-test-value")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.create_order.side_effect = RunStopped("stopped before send")
    monkeypatch.setattr("tradingagents.brokers.toss_executor.TossClient", Mock(return_value=client))
    path = main.LOCAL_DIR / "live_orders/orders.jsonl"
    with control.scope(), pytest.raises(RunStopped):
        asyncio.run(execute_plan(TossOrderPlan("BUY", "Buy", "005930", order_amount="100000"),
                                 execute=True, live_order_log=path))
    assert [json.loads(line)["status"] for line in path.read_text().splitlines()] == ["SUBMITTING", "CANCELLED_BEFORE_SUBMIT"]
    main.require_resolved_live_orders()
