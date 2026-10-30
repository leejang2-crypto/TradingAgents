from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import asyncio
import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import main
from tradingagents.research import postmarket as review
from tradingagents.brokers.position_risk import RiskStore, PositionRiskManager
from tradingagents.brokers.toss_executor import TossOrderPlan
from tests.test_position_risk import env


DAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 15, 35, tzinfo=main.KOREA_TZ)


def order(key, symbol, side, at, amount, *, quantity="10", fee="1", tax="0", context=None):
    return {"payload": {"clientOrderId": key, "symbol": symbol, "side": side, "quantity": quantity},
            "createdAt": at.isoformat(), "status": "FILLED", "orderId": key, "appliedQty": quantity,
            "strategyContext": context, "financialsIncomplete": False,
            "detail": {"orderId": key, "symbol": symbol, "side": side, "currency": "KRW", "status": "FILLED",
                       "execution": {"filledQuantity": quantity, "filledAmount": amount, "commission": fee,
                                     "tax": tax, "filledAt": at.isoformat()}}}


def history(settings=None, day=DAY, count=10):
    state = {"orders": {}, "positions": {}, "days": {}, "events": []}
    context = review.strategy_context(settings or {})
    for index in range(count):
        at = datetime.combine(day - timedelta(days=index % 3), datetime.min.time(), main.KOREA_TZ).replace(hour=10)
        symbol = f"{index + 1:06d}"
        state["orders"][f"b{index}"] = order(f"b{index}", symbol, "BUY", at, "1000", context=context)
        state["orders"][f"s{index}"] = order(f"s{index}", symbol, "SELL", at + timedelta(hours=1), "980", tax="2")
    return state


@pytest.fixture
def archive(tmp_path, monkeypatch):
    volume = tmp_path / "External"
    volume.mkdir()
    root = volume / "TradingAgents/postmarket"
    monkeypatch.setattr(review, "VOLUME", volume)
    monkeypatch.setattr(review, "ARCHIVE_ROOT", root)
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda path: path == volume or original(path))
    local = tmp_path / "local"
    risk = local / "positions/paper.sqlite3"
    store = RiskStore(risk)
    with store.locked():
        store.load()
        store.save(history())
    return local, risk, root


def test_confirmed_costed_round_trips_only():
    trades, opened, issues = review.reconstruct_trades(history(), DAY)
    assert len(trades) == 10 and not opened and not issues
    assert all(Decimal(trade["netPnl"]) == -24 for trade in trades)
    assert all(Decimal(trade["cost"]) == 1001 for trade in trades)


def test_partial_sells_form_one_round_trip_not_two_samples():
    state = history(count=1)
    state["orders"]["s0"] = order("s0", "000001", "SELL", NOW.replace(hour=11), "490", quantity="5")
    trades, opened, issues = review.reconstruct_trades(state, DAY)
    assert not trades and len(opened) == 1 and not issues
    state["orders"]["s1"] = order("s1", "000001", "SELL", NOW.replace(hour=12), "490", quantity="5")
    trades, opened, issues = review.reconstruct_trades(state, DAY)
    assert len(trades) == 1 and not opened and not issues
    assert Decimal(trades[0]["netPnl"]) == -23


@pytest.mark.parametrize("case", ["pending", "missing-fee", "wrong-symbol", "unknown-basis", "future-fill", "bad-quantity"])
def test_uncertain_records_never_create_training_rewards(case):
    state = history(count=1)
    sell = state["orders"]["s0"]
    if case == "pending": sell["status"] = "PENDING"
    if case == "missing-fee": sell["detail"]["execution"]["commission"] = None
    if case == "wrong-symbol": sell["detail"]["symbol"] = "123456"
    if case == "unknown-basis": del state["orders"]["b0"]
    if case == "future-fill": sell["detail"]["execution"]["filledAt"] = (NOW + timedelta(days=1)).isoformat()
    if case == "bad-quantity": sell["detail"]["execution"]["filledQuantity"] = "20"
    trades, _, issues = review.reconstruct_trades(state, DAY)
    assert not trades and issues


def test_policy_requires_sufficient_sample_and_only_reduces():
    trades, _, _ = review.reconstruct_trades(history(), DAY)
    policy = review.derive_policy(trades, DAY, "dry-run", "paper")
    assert len(policy["adjustments"]) == 1
    item = next(iter(policy["adjustments"].values()))
    assert item["multiplier"] == "0.75" and item["days"] == 3
    assert not review.derive_policy(trades[:9], DAY, "dry-run", "paper")["adjustments"]
    assert not review.derive_policy(trades, DAY, "dry-run", "paper", usable=False)["adjustments"]
    for row in trades:
        row["netPnl"] = "50"
    assert not review.derive_policy(trades, DAY, "dry-run", "paper")["adjustments"]


def test_old_unknown_strategy_is_not_inferred_from_current_settings():
    state = history()
    for row in state["orders"].values():
        row.pop("strategyContext", None)
    trades, _, _ = review.reconstruct_trades(state, DAY)
    assert len(trades) == 10
    assert not review.derive_policy(trades, DAY, "dry-run", "paper")["adjustments"]


def publish_policy(local, mode="dry-run", account="paper", settings=None, day=DAY):
    trades, _, _ = review.reconstruct_trades(history(settings, day), day)
    policy = review.derive_policy(trades, day, mode, account)
    review.atomic_json(review.state_path(local, mode, account), {"date": day.isoformat(), "policy": policy, "reportPath": "report.md"})


@pytest.mark.parametrize("case", ["same-day", "expired", "different-mode", "different-account", "changed-rules", "disabled", "corrupt"])
def test_application_is_time_mode_account_and_rule_scoped(archive, case):
    local, _, _ = archive
    publish_policy(local)
    now, mode, account, settings = NOW + timedelta(days=1), "dry-run", "paper", {}
    if case == "same-day": now = NOW
    if case == "expired": now += timedelta(days=7)
    if case == "different-mode": mode = "live"
    if case == "different-account": account = "other"
    if case == "changed-rules": settings["min_relative_volume"] = "2"
    if case == "disabled": settings["postmarket_apply"] = False
    if case == "corrupt":
        review.atomic_json(review.state_path(local, mode, account), {"policy": {"sourceDate": "invalid"}})
    assert review.sizing_adjustment(local, mode, account, now, settings)["multiplier"] == "1"


def test_archive_bundle_idempotency_and_policy_after_export(archive):
    local, risk, root = archive
    (local / ".env").write_text("SHOULD_NOT_BE_ARCHIVED=secret")
    result = review.run_review(local, "dry-run", DAY, risk, now=NOW, market={"status": "complete"})
    directory = Path(result["directory"])
    assert directory.is_relative_to(root) and result["status"] == "completed"
    assert {file.name for file in directory.iterdir()} == {"review.md", "review.json", "policy.json", "inputs.json.gz", "manifest.json", "opportunities.json"}
    with gzip.open(directory / "inputs.json.gz", "rt") as stream:
        text = stream.read()
    assert "SHOULD_NOT_BE_ARCHIVED" not in text and "riskState" in text
    repeat = review.run_review(local, "dry-run", DAY, risk, now=NOW, market={"status": "complete"})
    assert repeat["directory"] == result["directory"]
    assert review.sizing_adjustment(local, "dry-run", "paper", NOW + timedelta(days=1), {})["multiplier"] == "0.75"
    assert "표본" in Path(result["reportPath"]).read_text()
    review.run_review(local, "dry-run", DAY, risk, now=NOW)
    assert review.read_json(review.state_path(local, "dry-run", "paper"))["status"] == "completed"


def test_unmounted_volume_never_creates_internal_fallback(archive, monkeypatch):
    local, risk, root = archive
    monkeypatch.setattr(Path, "is_mount", lambda _: False)
    with pytest.raises(OSError, match="not mounted"):
        review.run_review(local, "dry-run", DAY, risk, now=NOW)
    assert not root.exists() and not review.state_path(local, "dry-run", "paper").exists()


def test_export_failure_never_publishes_policy(archive, monkeypatch):
    local, risk, _ = archive
    def fail(*_): raise OSError("disk unplugged")
    monkeypatch.setattr(review.os, "rename", fail)
    with pytest.raises(OSError):
        review.run_review(local, "dry-run", DAY, risk, now=NOW, market={"status": "complete"})
    assert not review.state_path(local, "dry-run", "paper").exists()


def test_historical_review_does_not_replace_newer_policy(archive):
    local, risk, _ = archive
    publish_policy(local, day=DAY + timedelta(days=1))
    review.run_review(local, "dry-run", DAY, risk, now=NOW)
    assert review.read_json(review.state_path(local, "dry-run", "paper"))["date"] == (DAY + timedelta(days=1)).isoformat()


def test_preclose_review_rejected_and_offline_review_does_not_teach(archive):
    local, risk, _ = archive
    with pytest.raises(ValueError, match="after"):
        review.run_review(local, "dry-run", DAY, risk, now=NOW.replace(hour=14))
    result = review.run_review(local, "dry-run", DAY, risk, now=NOW)
    assert result["status"] == "partial" and not result["policy"]["usable"]
    assert not result["policy"]["adjustments"]


def test_ledger_mode_cannot_be_mislabeled(archive):
    local, risk, _ = archive
    with pytest.raises(ValueError, match="mismatch"):
        review.run_review(local, "live", DAY, risk, now=NOW)


def test_actual_sizing_integration_reduces_only_new_buy(env):
    manager, broker, times, settings = env
    prior = times[0].date() - timedelta(days=1)
    publish_policy(manager.local_dir, "live", manager.store.path.stem, settings, prior)
    plan = TossOrderPlan(action="BUY", rating="Buy", symbol="005930", order_amount="1000")
    result = manager.execute(plan)
    assert result["payload"]["quantity"] == "7"
    saved = manager.snapshot()
    recorded = next(iter(saved["orders"].values()))
    assert recorded["strategyContext"]["sizingAdjustment"]["multiplier"] == "0.75"
    assert saved["positions"]["005930"]["quantity"] == "7"
    assert settings["position_target_return"] == ".01"


def setup_scheduler(archive, monkeypatch):
    local, risk_path, _ = archive
    monkeypatch.setattr(main, "LOCAL_DIR", local)
    args = main.make_parser().parse_args([])
    manager = object.__new__(PositionRiskManager)
    manager.live, manager.store, manager.backend = False, RiskStore(risk_path), Mock()
    args._position_manager = manager
    market = Mock(return_value={"status": "complete"})
    monkeypatch.setattr(review, "collect_market", market)
    return args, market


def test_scheduler_close_once_and_restart(archive, monkeypatch):
    args, market = setup_scheduler(archive, monkeypatch)
    asyncio.run(main.run_due_postmarket_review(args, NOW.replace(minute=34)))
    market.assert_not_called()
    asyncio.run(main.run_due_postmarket_review(args, NOW))
    market.assert_called_once()
    del args._postmarket_retry
    asyncio.run(main.run_due_postmarket_review(args, NOW + timedelta(minutes=20)))
    market.assert_called_once()


def test_scheduler_storage_failure_retries_without_disrupting_trading(archive, monkeypatch):
    args, market = setup_scheduler(archive, monkeypatch)
    available = review.archive_available
    monkeypatch.setattr(review, "archive_available", Mock(side_effect=OSError("denied")))
    asyncio.run(main.run_due_postmarket_review(args, NOW))
    market.assert_not_called()
    status = review.read_json(archive[0] / "postmarket/latest-dry-run.json")
    assert status["status"] == "retry-pending"
    monkeypatch.setattr(review, "archive_available", available)
    asyncio.run(main.run_due_postmarket_review(args, NOW + timedelta(minutes=15)))
    assert review.read_json(archive[0] / "postmarket/latest-dry-run.json")["status"] == "completed"


def test_extended_session_waits_for_exchange_close(archive, monkeypatch):
    args, market = setup_scheduler(archive, monkeypatch)
    market.return_value = {"status": "not-due", "dueAt": NOW.replace(hour=16).isoformat()}
    asyncio.run(main.run_due_postmarket_review(args, NOW))
    assert not review.state_path(archive[0], "dry-run", "paper").exists()
    assert args._postmarket_retry["after"].hour == 16


def test_summary_and_learning_records_are_deduplicated(archive):
    from tradingagents.agents.utils.experience import ExperienceStore
    local, risk, _ = archive
    store = ExperienceStore(local / "learning/experiences.sqlite3")
    run = store.start(mode="dry-run", ticker="005930.KS", trade_date=DAY.isoformat(), objective={}, routing={})
    contract = {"action": "HOLD", "reasons": ["volume"]}
    store.save_analysis(run, {"final_rating": "Hold", "intraday_contract": contract})
    main.atomic_write_json(local / "auto_paper" / f"auto_paper_{DAY}_once.json", {
        "date": DAY.isoformat(), "executionMode": "dry-run", "analyses": [{"learning": {"runId": run}, "intradayContract": contract}]})
    inputs = review.collect_inputs(local, "dry-run", DAY.isoformat(), risk)
    result = review.analyze_day(inputs, DAY, "dry-run", "paper")
    assert result["actions"] == {"HOLD": 1}


def test_dashboard_flags_and_report_escaping():
    assert main.validate_trading_settings({"postmarket_review": False, "postmarket_apply": True}) == {
        "postmarket_review": False, "postmarket_apply": True}
    with pytest.raises(ValueError):
        main.validate_trading_settings({"postmarket_apply": "true"})
    html = main.render_monitor_html({"postmarketReview": {"date": str(DAY), "status": "partial", "error": "<script>bad()</script>"}})
    assert 'name="postmarket_review"' in html and 'name="postmarket_apply"' in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html


def test_failure_without_run_id_is_not_double_counted(archive):
    from tradingagents.agents.utils.experience import ExperienceStore
    local, risk, _ = archive
    store = ExperienceStore(local / "learning/experiences.sqlite3")
    run = store.start(mode="dry-run", ticker="005930.KS", trade_date=str(DAY), objective={}, routing={})
    store.event(run, "ERROR", {"error": "bad news ticker"})
    main.atomic_write_json(local / "auto_paper" / f"auto_paper_{DAY}_once.json", {
        "date": str(DAY), "executionMode": "dry-run", "analyses": [{"ticker": "005930.KS", "error": "bad news ticker"}]})
    result = review.analyze_day(review.collect_inputs(local, "dry-run", str(DAY), risk), DAY, "dry-run", "paper")
    assert result["actions"] == {"ERROR": 1} and len(result["errors"]) == 1


def test_unreconciled_paper_fill_blocks_learning():
    inputs = {"riskState": history(), "summaries": [], "candidates": [], "market": {"status": "complete"},
              "paperFills": [{"id": "unlinked", "status": "SIMULATED_FILLED"}]}
    result = review.analyze_day(inputs, DAY, "dry-run", "paper")
    assert result["netRealizedPnl"] is None and result["dataIssues"]
    assert not result["policy"]["adjustments"]


def test_closed_and_extended_sessions_do_not_fetch_candles():
    backend = Mock()
    backend.clock.return_value = NOW
    backend.calendar.return_value = {"today": {"date": str(DAY), "integrated": None}}
    assert review.collect_market(backend, DAY)["status"] == "closed"
    backend.call.assert_not_called()
    backend.calendar.return_value["today"]["integrated"] = {"regularMarket": {
        "startTime": NOW.replace(hour=9, minute=0).isoformat(), "endTime": NOW.replace(hour=16, minute=30).isoformat()}}
    assert review.collect_market(backend, DAY)["status"] == "not-due"
    backend.call.assert_not_called()
