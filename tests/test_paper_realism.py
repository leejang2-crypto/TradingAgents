import asyncio
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock

import pytest

import main
from tests.test_position_risk import FakeBroker, buy
from tradingagents.agents.utils.agent_utils import get_trading_objective_instruction
from tradingagents.brokers.account_service import paper_account, paper_history
from tradingagents.brokers.paper_account import KST
from tradingagents.brokers.position_risk import BrokerBackend, PositionRiskManager
from tradingagents.brokers.toss_executor import TossOrderPlan


@pytest.fixture
def paper(tmp_path, monkeypatch):
    for name in ("TRADINGAGENTS_PAPER_FEE_RATE", "TRADINGAGENTS_PAPER_SELL_TAX_RATE", "TRADINGAGENTS_PAPER_SLIPPAGE_RATE"):
        monkeypatch.setenv(name, "0")
    clock = lambda: datetime(2026, 9, 15, 10, tzinfo=KST)
    fake = FakeBroker(clock)
    backend = BrokerBackend(tmp_path, False, "1000", clock=clock)
    backend.prices, backend.calendar, backend.entry_signal = fake.prices, fake.calendar, fake.entry_signal
    backend.call = Mock(side_effect=AssertionError("No live broker calls"))
    settings = {"paper_capital": "10000", "position_pct": "1", "trade_risk_fraction": ".02",
                "daily_loss_limit": ".5", "estimated_cost_rate": "0", "stagnation_minutes": 360}
    account = paper_account(tmp_path, "1000")
    manager = PositionRiskManager(tmp_path, backend=backend, settings=lambda: settings, clock=clock)
    return manager, account, backend, fake, settings, clock


def test_buy_uses_actual_cash_not_larger_configured_capital(paper):
    manager, account, backend, _, _, clock = paper
    # The configured capital is 10,000, but the existing account only has 1,000.
    result = manager.execute(TossOrderPlan("BUY", "Buy", "005930", order_amount="10000"))
    assert result["payload"]["quantity"] == "10"
    assert account.view(clock())["cash"] == "0.00"
    assert buy(manager, "000660")["skipped"] == "intraday-filter"
    assert len(account.view(clock())["trades"]) == 1
    backend.call.assert_not_called()


def test_cash_sizing_includes_configured_fees(paper):
    manager, account, _, _, _, clock = paper
    with account.connect() as db:
        db.execute("UPDATE account SET fee_rate='0.01'")
    result = manager.execute(TossOrderPlan("BUY", "Buy", "005930", order_amount="10000"))
    assert result["payload"]["quantity"] == "9"
    assert account.view(clock())["cash"] == "91.00"


def test_unheld_sell_and_duplicate_buy_cannot_create_fake_trades(paper):
    manager, account, _, _, _, clock = paper
    sell = TossOrderPlan("SELL", "Sell", "005930", quantity="1")
    assert manager.execute(sell)["skipped"] == "no-managed-holdings"
    assert account.view(clock())["trades"] == []
    buy(manager)
    with pytest.raises(ValueError, match="추가 매수"):
        buy(manager)
    assert len(account.view(clock())["trades"]) == 1
    result = manager.execute(sell)
    assert result["payload"]["quantity"] == "10"
    assert account.view(clock())["positions"] == []
    assert manager.execute(sell)["skipped"] == "no-managed-holdings"
    assert len(account.view(clock())["trades"]) == 2


def test_paper_fills_after_initial_import_are_reconciled_once(paper):
    manager, account, backend, _, _, clock = paper
    manager.tick()
    account.fill(TossOrderPlan("BUY", "Buy", "005930", quantity="2"), backend.prices(["005930"])["005930"], "external", now=clock())
    manager.tick()
    manager.tick()
    assert manager.snapshot()["positions"]["005930"]["quantity"] == "2"
    assert Decimal(manager.snapshot()["cashDelta"]) == -200
    with pytest.raises(ValueError, match="추가 매수"):
        buy(manager)
    result = manager.execute(TossOrderPlan("SELL", "Sell", "005930", quantity="1"))
    assert result["payload"]["quantity"] == "2"
    assert account.view(clock())["cash"] == "1000.00"
    manager.tick()
    assert manager.snapshot()["cashDelta"] == "0.00"


def test_committed_paper_fill_recovers_lost_local_ack_without_replaying(paper):
    manager, account, backend, _, _, clock = paper
    submit = backend.submit
    def lost_ack(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("local acknowledgement lost")
    backend.submit = lost_ack
    assert buy(manager)["paperExecution"]["status"] == "SIMULATED_FILLED"
    assert len(account.view(clock())["trades"]) == 1
    manager.tick()
    assert not manager.snapshot()["unconfirmed"]
    assert manager.snapshot()["positions"]["005930"]["quantity"] == "10"
    assert len(account.view(clock())["trades"]) == 1


def test_fill_audit_and_limit_validation_are_transactional(paper):
    _, account, backend, _, _, clock = paper
    quote = backend.prices(["005930"])["005930"]
    limit = TossOrderPlan("BUY", "Buy", "005930", quantity="2", order_type="LIMIT")
    assert account.fill(limit, quote, "over-limit", now=clock(), limit_price="99")["status"] == "SIMULATED_REJECTED"
    assert account.fill(limit, quote, "no-limit", now=clock())["status"] == "SIMULATED_REJECTED"
    assert account.view(clock())["positions"] == []
    filled = account.fill(limit, quote, "buy", now=clock(), limit_price="100")
    assert (filled["cashBefore"], filled["cashAfter"]) == ("1000.00", "800.00")
    assert (filled["quantityBefore"], filled["quantityAfter"]) == ("0", "2")
    assert filled["grossAmount"] == "200.00"
    sell = TossOrderPlan("SELL", "Sell", "005930", quantity="3")
    rejected = account.fill(sell, quote, "too-many", now=clock())
    assert rejected["status"] == "SIMULATED_REJECTED"
    assert rejected["cashBefore"] == rejected["cashAfter"] == "800.00"
    assert rejected["quantityBefore"] == rejected["quantityAfter"] == "2"
    assert account.fill(limit, quote, "buy", now=clock(), limit_price="100") == filled


def test_history_uses_sqlite_evidence_and_retains_legacy_without_claiming_fills(paper):
    manager, account, _, _, _, clock = paper
    buy(manager)
    journal_path = manager.local_dir / "paper_trades/trades.jsonl"
    original = journal_path.read_bytes()
    rows = [json.loads(line) for line in journal_path.read_text().splitlines()]
    legacy = {"recordedAt": "2026-08-20T09:00:00+09:00", "status": "FILLED", "fillVerified": True,
              "plan": {"symbol": "000880", "action": "SELL", "quantity": "1"},
              "simulation": {"id": "not-in-ledger", "status": "SIMULATED_FILLED", "quantity": "1"}}
    result = paper_history(manager.local_dir, [legacy, *rows, *rows])
    assert len(result) == 2
    assert result[0]["status"] == "RECORDED" and not result[0]["ledgerBacked"]
    assert not result[0]["fillVerified"] and result[0]["simulation"] is None
    assert result[1]["ledgerBacked"] and result[1]["simulation"]["quantity"] == "10"
    assert journal_path.read_bytes() == original
    assert len(account.view(clock())["trades"]) == 1
    # Recover a committed fill for display even if a JSONL append was lost.
    assert paper_history(manager.local_dir, [])[0]["simulation"]["quantity"] == "10"
    html = main.render_monitor_html({"recentTrades": result})
    assert "주문 계획만 기록 · 계좌 미반영" in html and "가상 체결 · 계좌 반영" in html
    assert "처리 후 보유 수량" in html and "처리 후 현금" in html


def test_account_context_is_bounded_and_included_in_decision_prompt(paper, monkeypatch):
    manager, account, _, _, _, clock = paper
    buy(manager)
    context = account.trading_context("005930", now=clock())
    assert context["cash"] == "0.00" and context["heldQuantity"] == "10"
    assert context["positionCount"] == 1 and "trades" not in context
    monkeypatch.setattr("tradingagents.dataflows.config.get_config", lambda: {"account_context": context})
    prompt = get_trading_objective_instruction()
    assert "Shares held in this symbol: 10" in prompt and "Available cash: 0.00" in prompt
    assert "no short selling" in prompt


def test_main_passes_paper_account_snapshot_without_executing_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    graph = Mock()
    graph.propagate.return_value = ({"final_trade_decision": "**Rating**: Hold"}, "Hold")
    graph.save_reports.return_value = "report.md"
    factory = Mock(return_value=graph)
    monkeypatch.setattr(main, "TradingAgentsGraph", factory)
    args = main.make_parser().parse_args(["--scenario", "korea-dry-run", "--date", main.today_korea().isoformat()])
    args._analysis_only = True
    paper_account(tmp_path, "1234")
    result = asyncio.run(main.run_korea_toss(args, execute=False))
    context = factory.call_args.kwargs["config"]["account_context"]
    assert context["cash"] == "1234.00" and context["heldQuantity"] == "0"
    assert result["toss"]["skipped"] == "analysis-only"


def test_concurrent_orders_cannot_overdraw_paper_cash(paper):
    _, account, backend, _, _, clock = paper
    quote = backend.prices(["005930"])["005930"]
    plan = TossOrderPlan("BUY", "Buy", "005930", quantity="8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: account.fill(plan, quote, key, now=clock()), ["first", "second"]))
    assert sorted(row["status"] for row in results) == ["SIMULATED_FILLED", "SIMULATED_REJECTED"]
    assert account.view(clock())["cash"] == "200.00"
    assert account.view(clock())["positions"][0]["quantity"] == "8"


def test_old_price_cannot_execute_even_if_valid_for_valuation(paper):
    _, account, backend, _, _, clock = paper
    quote = backend.prices(["005930"])["005930"]
    quote["timestamp"] = (clock() - timedelta(seconds=121)).isoformat()
    fill = account.fill(TossOrderPlan("BUY", "Buy", "005930", quantity="1"), quote, "stale", now=clock())
    assert fill["status"] == "SIMULATED_REJECTED" and "stale" in fill["reason"]
    assert account.view(clock())["cash"] == "1000.00"
