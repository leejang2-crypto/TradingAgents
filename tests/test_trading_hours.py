import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

import main
from tests.test_position_risk import env, buy
from tests.test_twice_daily_schedule import clock, StopScheduler
from tradingagents.brokers.paper_account import KST
from tradingagents.brokers.position_risk import PositionRiskManager
from tradingagents.brokers.toss_client import OrderWindowClosed
from tradingagents.brokers.toss_executor import TossOrderPlan
from tradingagents.monitoring.summary import saved_objective
from tradingagents.agents.utils.agent_utils import get_trading_objective_instruction
from tradingagents.dataflows.config import run_config


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('hour,minute,second,allowed', [
    (9, 59, 59, False), (10, 0, 0, True), (14, 29, 59, True),
    (14, 30, 0, False), (15, 0, 0, False),
])
def test_buy_window_boundaries_in_both_modes(env, live, hour, minute, second, allowed):
    manager, broker, now, _ = env
    manager.live = live
    broker.affordable_quantity = lambda price: Decimal(1000)
    now[0] = now[0].replace(hour=hour, minute=minute, second=second)
    if allowed:
        buy(manager)
        assert len(broker.orders) == 1
    else:
        broker.entry_signal = Mock(side_effect=AssertionError('No chart reads outside entry window'))
        with pytest.raises(ValueError, match='매수 제한'):
            buy(manager)
        broker.entry_signal.assert_not_called()
        assert not broker.orders


@pytest.mark.parametrize('hour,minute', [(9, 59), (14, 30)])
def test_last_submit_guard_cannot_be_bypassed(env, hour, minute):
    manager, broker, now, _ = env
    now[0] = now[0].replace(hour=hour, minute=minute)
    state = manager.store.load()
    payload = {'clientOrderId': 'test-window', 'symbol': '005930', 'side': 'BUY',
               'quantity': '1', 'orderType': 'LIMIT', 'price': '100', 'timeInForce': 'DAY'}
    with pytest.raises(OrderWindowClosed):
        manager._submit(state, payload, 'test')
    assert not broker.orders and state['orders']['test-window']['status'] == 'NOT_SENT'


def test_settings_change_during_chart_read_blocks_entry(env):
    manager, broker, _, settings = env
    original = broker.entry_signal
    def changed(*args):
        signal = original(*args)
        settings['buy_start_minutes'] = 90
        return signal
    broker.entry_signal = changed
    with pytest.raises(ValueError, match='매수 제한'):
        buy(manager)
    assert not broker.orders


def delayed_open(broker):
    original = broker.calendar
    def calendar(day):
        result = original(day)
        session = (result['today'].get('integrated') or {}).get('regularMarket')
        if session:
            session['startTime'] = day + 'T10:00:00+09:00'
            session['endTime'] = day + 'T16:30:00+09:00'
        return result
    broker.calendar = calendar


def test_actual_exchange_open_anchors_buy_and_scan(env):
    manager, broker, now, settings = env
    delayed_open(broker)
    settings['market_close'] = '16:30'
    with pytest.raises(ValueError, match='매수 제한'):
        buy(manager)
    args = main.make_parser().parse_args(['--market-close', '16:30'])
    args._position_manager = manager
    window = main.scheduled_entry_window(now[0], args)
    assert window == (now[0].replace(hour=11), now[0].replace(hour=15, minute=30))
    assert main.active_trading_slot(now[0].replace(hour=11, minute=35), args, window) == 'scan-1100'
    now[0] = now[0].replace(hour=11)
    buy(manager)
    assert len(broker.orders) == 1


@pytest.mark.parametrize('hour,minute,allowed', [(9, 5, True), (14, 45, True), (15, 29, True), (15, 30, False)])
def test_sell_remains_available_outside_buy_window(env, hour, minute, allowed):
    manager, broker, now, _ = env
    buy(manager)
    now[0] += timedelta(days=1)
    now[0] = now[0].replace(hour=hour, minute=minute)
    plan = TossOrderPlan('SELL', 'Sell', '005930')
    if allowed:
        manager.execute(plan)
        assert broker.orders['broker-2']['side'] == 'SELL'
    else:
        with pytest.raises(ValueError, match='정규장 밖'):
            manager.execute(plan)
        assert len(broker.orders) == 1


@pytest.mark.parametrize('live', [False, True])
def test_carry_deadline_skips_holiday_and_survives_restart(env, live):
    manager, broker, now, settings = env
    manager.live = live
    broker.affordable_quantity = lambda price: Decimal(1000)
    now[0] = datetime(2026, 9, 18, 10, tzinfo=KST)
    broker.holidays.add(date(2026, 9, 21))
    buy(manager)
    manager.carry('005930', manager.snapshot()['positions']['005930']['cycle'], True)
    broker.price = Decimal('100.3')  # No target, stop or stagnation exit.
    now[0] = now[0].replace(hour=15, minute=20)
    manager.tick()
    assert len(broker.orders) == 1
    # Use the original store identity while exercising persistent state reload.
    restarted = PositionRiskManager(manager.local_dir, live=live, backend=broker,
                                    settings=lambda: settings, clock=lambda: now[0])
    restarted.store = manager.store
    now[0] = datetime(2026, 9, 22, 9, 44, 59, tzinfo=KST)
    restarted.tick()
    p = restarted.snapshot()['positions']['005930']
    assert p['liquidationStartsAt'] == '2026-09-22T09:45:00+09:00'
    assert p['liquidationDeadlineAt'] == '2026-09-22T10:00:00+09:00'
    assert len(broker.orders) == 1
    now[0] += timedelta(seconds=1)
    restarted.tick()
    assert broker.orders['broker-2']['side'] == 'SELL'
    assert broker.orders['broker-2']['quantity'] == '10'
    restarted.tick()
    assert restarted.snapshot()['positions']['005930']['quantity'] == '0'


def test_delayed_open_also_delays_carry_deadline(env):
    manager, broker, now, settings = env
    buy(manager)
    manager.carry('005930', manager.snapshot()['positions']['005930']['cycle'], True)
    delayed_open(broker)
    manager.calendars.clear()
    settings['market_close'] = '16:30'
    broker.price = Decimal('100.3')
    now[0] += timedelta(days=1)
    now[0] = now[0].replace(hour=10, minute=44)
    manager.tick()
    assert len(broker.orders) == 1
    assert manager.snapshot()['positions']['005930']['liquidationDeadlineAt'].endswith('11:00:00+09:00')
    now[0] = now[0].replace(minute=45)
    manager.tick()
    assert len(broker.orders) == 2


def test_unfilled_carry_exit_is_flagged_without_duplicate_order(env):
    manager, broker, now, _ = env
    buy(manager)
    manager.carry('005930', manager.snapshot()['positions']['005930']['cycle'], True)
    broker.price, broker.auto_fill = Decimal('100.3'), False
    now[0] += timedelta(days=1)
    now[0] = now[0].replace(hour=9, minute=45)
    manager.tick()
    now[0] = now[0].replace(hour=10, minute=0)
    manager.tick()
    p = manager.snapshot()['positions']['005930']
    assert p['deadlineExceeded'] and p['quantity'] == '10'
    assert p['alert'] == '기존 주문 체결/취소 확인 대기'
    assert len(broker.orders) == 2 and broker.cancels == ['broker-2']
    broker.fill('broker-2', Decimal(10), 'FILLED')
    manager.tick()
    assert manager.snapshot()['positions']['005930']['quantity'] == '0'


def test_unfilled_same_day_exit_remains_overdue_after_close(env):
    manager, broker, now, _ = env
    buy(manager)
    broker.auto_fill = False
    now[0] = now[0].replace(hour=15, minute=15)
    manager.tick()
    now[0] = now[0].replace(minute=30)
    manager.tick()
    state = manager.snapshot()
    assert not state['marketOpen'] and state['positions']['005930']['deadlineExceeded']
    assert state['positions']['005930']['quantity'] == '10' and len(broker.orders) == 2


@pytest.mark.parametrize('hour,minute', [(9, 59), (14, 30), (15, 20)])
def test_scheduler_does_not_analyze_outside_buy_window(clock, monkeypatch, hour, minute):
    clock.current = clock.current.replace(hour=hour, minute=minute)
    args = main.make_parser().parse_args([])
    runner = AsyncMock()
    monkeypatch.setattr(main, 'run_auto_trading', runner)
    async def stop(_):
        raise StopScheduler
    monkeypatch.setattr(main.asyncio, 'sleep', stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    runner.assert_not_called()


def test_scheduler_calendar_failure_does_not_start_analysis(clock, monkeypatch):
    clock.current = clock.current.replace(hour=10)
    args = main.make_parser().parse_args([])
    monkeypatch.setattr(main, 'scheduled_entry_window', Mock(side_effect=ValueError('missing calendar')))
    runner = AsyncMock()
    monkeypatch.setattr(main, 'run_auto_trading', runner)
    async def stop(_):
        raise StopScheduler
    monkeypatch.setattr(main.asyncio, 'sleep', stop)
    with pytest.raises(StopScheduler):
        asyncio.run(main.run_daily_market_scheduler(args))
    runner.assert_not_called()
    assert main.build_monitor_snapshot()['runtime']['status'] == 'market-calendar-unavailable'


@pytest.mark.parametrize('value', [0, -1, 61, True, 'nan'])
def test_carry_limit_cannot_exceed_one_hour(value):
    with pytest.raises(ValueError):
        main.validate_trading_settings({'carry_exit_minutes': value})


def test_web_settings_persist_and_report_policy_matches(clock):
    args = main.make_parser().parse_args([])
    saved = main.initialize_trading_settings(args)
    assert [saved[key] for key in ('buy_start_minutes', 'buy_cutoff_minutes', 'candidate_scan_minutes', 'carry_exit_minutes')] == [60] * 4
    assert saved['position_check_seconds'] == 30
    main.save_trading_settings({'buy_start_minutes': '90', 'candidate_scan_minutes': '30'})
    main.apply_trading_settings(args)
    assert args.buy_start_minutes == 90 and args.candidate_scan_minutes == 30
    objective = main.trading_objective(args)
    assert objective['buy_start'] == '10:30' and objective['buy_cutoff'] == '14:30'
    assert objective['carry_exit_time'] == '10:00'
    with run_config({'trading_objective': objective}):
        prompt = get_trading_objective_instruction()
    assert 'within 60 minutes' in prompt and '10:30' in prompt and 'every 30 minutes' in prompt
    lines = saved_objective({'tradingObjective': objective})['lines']
    assert any('개장 후 60분 이내' in line for line in lines)
    assert any('30분 간격' in line for line in lines)
    html = main.render_monitor_html({'settings': saved})
    for key in ('buy_start_minutes', 'candidate_scan_minutes', 'carry_exit_minutes'):
        assert f'name="{key}"' in html


def test_invalid_entry_window_rejected():
    with pytest.raises(ValueError, match='매수 시작'):
        main.validate_trading_settings({'market_open': '09:00', 'market_close': '11:00'})
