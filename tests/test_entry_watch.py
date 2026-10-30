from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tests.test_position_risk import FakeBroker
from tradingagents.brokers.paper_account import KST
from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.brokers.toss_executor import TossOrderPlan


@pytest.fixture(params=[False, True], ids=['paper', 'live'])
def watching(request, tmp_path, monkeypatch):
    monkeypatch.setenv('TOSS_CLIENT_ID', 'fake-client')
    monkeypatch.setenv('TOSS_ACCOUNT_SEQ', 'fake-account')
    time = [datetime(2026, 9, 15, 10, tzinfo=KST)]
    clock = lambda: time[0]
    broker = FakeBroker(clock)
    broker.affordable_quantity = lambda price: Decimal(1000)
    settings = {'paper_capital': '10000', 'estimated_cost_rate': '0', 'entry_watch_minutes': 30,
                'candidate_scan_minutes': 5}
    manager = PositionRiskManager(tmp_path, live=request.param, backend=broker, settings=lambda: settings, clock=clock)
    signal = broker.entry_signal
    ready = [False]
    def entry(*args):
        result = signal(*args)
        result.update(allowed=ready[0], reasons=[] if ready[0] else ['돌파 조건 대기'])
        return result
    broker.entry_signal = entry
    return manager, broker, time, settings, ready


def queue(manager, symbol='005930'):
    return manager.execute_with_watch(TossOrderPlan('BUY', 'Buy', symbol, order_amount='1000'))


def test_buy_waits_for_new_completed_bar_then_submits_once(watching):
    manager, broker, time, _, ready = watching
    result = queue(manager)
    assert result['entryWatch']['status'] == 'waiting'
    assert not broker.orders
    ready[0] = True
    manager.entry_watch.process()
    assert not broker.orders  # Already checked the current completed bar.
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    assert len(broker.orders) == 1
    assert manager.snapshot()['entryWatches']['005930']['status'] == 'submitted'
    assert 'settingsSignature' not in manager.snapshot()['entryWatches']['005930']
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    assert len(broker.orders) == 1


def test_unmet_conditions_stay_waiting_without_duplicate_checks(watching):
    manager, broker, time, _, _ = watching
    queue(manager)
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    manager.entry_watch.process()
    row = manager.snapshot()['entryWatches']['005930']
    assert row['attempts'] == 1 and row['status'] == 'waiting' and not broker.orders


@pytest.mark.parametrize('ttl', [30, 120])
def test_hourly_watch_does_not_submit_between_scheduled_checks(watching, ttl):
    manager, broker, time, settings, ready = watching
    settings.update(candidate_scan_minutes=60, entry_watch_minutes=ttl)
    queue(manager)
    ready[0] = True
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    assert not broker.orders
    time[0] += timedelta(minutes=55)
    manager.entry_watch.process()
    row = manager.snapshot()['entryWatches']['005930']
    assert row['status'] == ('submitted' if ttl == 120 else 'cancelled')
    assert len(broker.orders) == (1 if ttl == 120 else 0)


@pytest.mark.parametrize('reason', ['timeout', 'day-change', 'settings', 'paused', 'stop', 'restart'])
def test_invalidation_never_submits(watching, reason):
    manager, broker, time, settings, ready = watching
    queue(manager)
    ready[0] = True
    time[0] += timedelta(minutes=5)
    if reason == 'timeout': time[0] += timedelta(minutes=25)
    if reason == 'day-change': time[0] += timedelta(days=1)
    if reason == 'settings': settings['position_target_return'] = '.02'
    if reason == 'paused': manager.entry_watch.guard = lambda: 'cancel'
    if reason == 'stop': manager.stop_event.set()
    if reason == 'restart':
        manager = PositionRiskManager(manager.local_dir, live=manager.live, backend=broker,
                                      settings=lambda: settings, clock=lambda: time[0])
    manager.entry_watch.process()
    assert not broker.orders
    assert manager.snapshot()['entryWatches']['005930']['status'] == 'cancelled'


def test_hold_or_review_cannot_create_watch_and_replaces_previous_buy(watching):
    manager, broker, _, _, _ = watching
    queue(manager)
    manager.execute_with_watch(TossOrderPlan('HOLD', 'Hold', '005930'))
    manager.execute_with_watch(TossOrderPlan('REVIEW', 'REVIEW', '000660'))
    watches = manager.snapshot()['entryWatches']
    assert watches['005930']['status'] == 'cancelled' and '000660' not in watches
    assert not broker.orders


def test_unknown_submission_is_not_retried(watching):
    manager, broker, time, _, ready = watching
    queue(manager)
    ready[0] = True
    time[0] += timedelta(minutes=5)
    broker.fail_submit = True
    manager.entry_watch.process()
    assert len(broker.orders) == 1
    assert manager.snapshot()['entryWatches']['005930']['status'] == 'cancelled'
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    assert len(broker.orders) == 1


def test_pause_during_slow_signal_fetch_blocks_submission(watching):
    manager, broker, time, _, ready = watching
    queue(manager)
    ready[0] = True
    signal = broker.entry_signal
    def pause(*args):
        result = signal(*args)
        manager.entry_watch.guard = lambda: 'cancel'
        return result
    broker.entry_signal = pause
    time[0] += timedelta(minutes=5)
    manager.entry_watch.process()
    assert not broker.orders


def test_cancel_is_versioned_and_queue_is_bounded(watching):
    manager, broker, time, _, ready = watching
    first = queue(manager)['entryWatch']
    replacement = queue(manager)['entryWatch']
    with pytest.raises(ValueError):
        manager.entry_watch.cancel('005930', expected_id=first['id'])
    manager.entry_watch.cancel('005930', expected_id=replacement['id'])
    for symbol in ['000001', '000002', '000003', '000004', '000005']:
        assert queue(manager, symbol).get('entryWatch')
    assert not queue(manager, '000006').get('entryWatch')
    assert not broker.orders


def test_holding_exits_continue_while_entry_watch_is_waiting(watching):
    manager, broker, _, _, ready = watching
    ready[0] = True
    manager.execute(TossOrderPlan('BUY', 'Buy', '000660', order_amount='1000'))
    ready[0] = False
    queue(manager)
    broker.price = Decimal(101)
    manager.tick()
    assert manager.snapshot()['positions']['000660']['exitReason'] == 'position-target'
    assert manager.snapshot()['entryWatches']['005930']['status'] == 'waiting'


def test_disabling_watch_and_existing_account_holdings_are_respected(watching):
    manager, broker, _, settings, _ = watching
    settings['entry_watch_minutes'] = 0
    assert 'entryWatch' not in queue(manager)
    broker.positions['005930'] = Decimal(2)
    with pytest.raises(ValueError, match='추가 매수'):
        queue(manager)


def test_active_analysis_delays_watch_without_consuming_bar(watching):
    manager, broker, time, _, ready = watching
    queue(manager)
    ready[0] = True
    time[0] += timedelta(minutes=5)
    manager.entry_watch.guard = lambda: 'wait'
    manager.entry_watch.process()
    assert not broker.orders and manager.snapshot()['entryWatches']['005930']['attempts'] == 0
    manager.entry_watch.guard = lambda: 'ready'
    manager.entry_watch.process()
    assert len(broker.orders) == 1


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('run_mode', ['once', 'daily-market'])
def test_main_routes_only_continuous_runs_to_watch(tmp_path, monkeypatch, live, run_mode):
    import asyncio
    from unittest.mock import Mock
    import main

    monkeypatch.setattr(main, 'LOCAL_DIR', tmp_path)
    monkeypatch.setattr(main, 'collect_korea_news_snapshot', lambda args: None)
    monkeypatch.setattr(main, 'is_regular_market_open', lambda *args, **kwargs: True)
    args = main.make_parser().parse_args([
        '--scenario', 'korea-execute' if live else 'korea-dry-run', '--yes',
        '--run-mode', run_mode, '--date', '2026-09-15',
    ])
    graph = Mock()
    graph.propagate.return_value = ({'news_report': 'fixture', 'final_trade_decision': '**Rating**: Buy'}, 'Buy')
    graph.save_reports.return_value = 'report.md'
    monkeypatch.setattr(main, 'TradingAgentsGraph', Mock(return_value=graph))
    risk = Mock(spec=PositionRiskManager)
    risk.entry_watch = Mock()
    risk.execute.return_value = {'executed': live, 'dryRun': not live}
    risk.execute_with_watch.return_value = {'executed': live, 'dryRun': not live}
    args._position_manager = risk
    asyncio.run(main.run_korea_toss(args, execute=live))
    risk.entry_watch.cancel.assert_called_once_with(args.korea_ticker.split('.')[0], '새 분석 시작')
    chosen = risk.execute_with_watch if run_mode == 'daily-market' else risk.execute
    other = risk.execute if run_mode == 'daily-market' else risk.execute_with_watch
    chosen.assert_called_once()
    other.assert_not_called()
