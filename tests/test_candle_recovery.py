from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from tests.test_intraday import data
from tests.test_candidate_screen import Market, screen
from tradingagents.brokers import intraday
from tradingagents.brokers.intraday import CandleDataError, bars, session_candles
from tradingagents.brokers.toss_client import TossApiError


@pytest.mark.parametrize('index', [False, True])
@pytest.mark.parametrize('page_size', [50, 100, 200])
def test_afternoon_small_pages_reach_opening_without_assuming_200(data, index, page_size):
    start = data[3]
    now = start + timedelta(minutes=306, seconds=37)
    offset = timedelta(minutes=1) if index else timedelta(0)
    rows = [{**data[0][0], 'timestamp': (start + timedelta(minutes=i) - offset).isoformat()}
            for i in range(1, 307)]
    calls = []
    def call(method, symbol, **kwargs):
        calls.append(kwargs)
        page = [r for r in reversed(rows) if datetime.fromisoformat(r['timestamp']) < datetime.fromisoformat(kwargs['before'])][:page_size]
        return {'candles': page, 'nextBefore': page[-1]['timestamp'] if page else None}
    diagnostics = {}
    result = session_candles(call, 'KOSPI' if index else '005930', start, now, index=index, diagnostics=diagnostics)
    five, _, _ = bars(result, start, now, index=index)
    assert len(result) == 305 and len(five) == 61
    assert diagnostics['code'] == 'complete' and diagnostics['missingCount'] == 0
    assert diagnostics['pageCount'] == len(calls)
    if page_size == 50: assert len(calls) > 3
    assert len(calls) <= 12


@pytest.mark.parametrize('index', [False, True])
def test_delayed_last_minute_is_refetched_without_changing_snapshot(data, monkeypatch, index):
    rows, _, _, start, now = data
    offset = timedelta(minutes=1) if index else timedelta(0)
    raw = [{**r, 'timestamp': (datetime.fromisoformat(r['timestamp']) - offset).isoformat()} for r in rows]
    calls = []
    sleep = Mock()
    monkeypatch.setattr(intraday.time, 'sleep', sleep)
    def call(*args, **kwargs):
        calls.append(kwargs)
        return {'candles': raw[:-1] if len(calls) < 3 else raw, 'nextBefore': None}
    diagnostics = {}
    result = session_candles(call, 'test', start, now, index=index, diagnostics=diagnostics)
    assert len(result) == 20 and diagnostics['retryCount'] == 2
    assert diagnostics['expectedEnd'] == rows[-1]['timestamp']
    assert sleep.call_count == 2 and len(calls) == 3


def test_missing_middle_minute_has_one_targeted_repair(data):
    rows, _, _, start, now = data
    calls = []
    def call(*args, **kwargs):
        calls.append(kwargs)
        return {'candles': rows[:8] + rows[9:] if len(calls) == 1 else [rows[8]], 'nextBefore': None}
    assert len(session_candles(call, '005930', start, now)) == 20
    assert calls[1]['before'] == (start + timedelta(minutes=10)).isoformat()


def test_persistent_gap_stays_blocked_with_specific_diagnostics(data):
    rows, _, _, start, now = data
    call = Mock(return_value={'candles': rows[:8] + rows[9:], 'nextBefore': None})
    with pytest.raises(CandleDataError) as error:
        session_candles(call, 'KOSPI-test', start, now)
    diagnostics = error.value.diagnostics
    assert diagnostics['missingAt'] == [rows[8]['timestamp']]
    assert diagnostics['expectedCount'] == 20 and diagnostics['receivedCount'] == 19
    assert diagnostics['firstAt'] == rows[0]['timestamp'] and diagnostics['lastAt'] == rows[-1]['timestamp']
    assert call.call_count == 2
    assert '누락 1개' in str(error.value)


def test_before_twenty_minutes_does_not_query_and_reports_wait(data):
    start = data[3]
    call = Mock()
    with pytest.raises(CandleDataError) as error:
        session_candles(call, 'KOSPI', start, start + timedelta(minutes=19), index=True)
    assert error.value.diagnostics['code'] == 'too-early'
    assert '최소 완성 분봉 대기' in str(error.value)
    call.assert_not_called()


def test_misaligned_seconds_are_diagnosed_not_silently_rounded(data):
    rows, _, _, start, now = deepcopy(data)
    rows[4]['timestamp'] = (start + timedelta(minutes=5, seconds=1)).isoformat()
    call = Mock(return_value={'candles': rows, 'nextBefore': None})
    with pytest.raises(CandleDataError) as error:
        session_candles(call, 'KOSPI', start, now)
    assert error.value.diagnostics['code'] == 'unaligned'
    assert error.value.diagnostics['unexpectedCount'] == 1
    assert call.call_count == 1


@pytest.mark.parametrize('code', [401, 403, 429])
def test_api_restrictions_are_not_retried_by_recovery(data, code):
    call = Mock(side_effect=TossApiError(code, 'fixture'))
    with pytest.raises(TossApiError): session_candles(call, '005930', data[3], data[4])
    assert call.call_count == 1


def test_conflicting_completed_bar_on_retry_is_rejected(data):
    rows, _, _, start, now = deepcopy(data)
    calls = []
    def call(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1: return {'candles': rows[:8] + rows[9:], 'nextBefore': None}
        return {'candles': [rows[8], {**rows[4], 'volume': '999'}], 'nextBefore': None}
    with pytest.raises(ValueError, match='Conflicting duplicate'):
        session_candles(call, '005930', start, now)


def test_market_failure_exposes_diagnostics_in_candidate_result(data):
    market = Market(data)
    market.rows.pop(5)
    result = screen(data, market)
    assert result['status'] == 'failed' and result['eligibleCount'] == 0
    assert result['dataDiagnostics']['symbol'] == 'KOSPI'
    assert result['marketData']['missingCount'] == 1
    assert result['marketData']['pageCount'] == 2


def test_pagination_is_bounded_even_for_tiny_pages(data):
    start = data[3]
    now = start + timedelta(minutes=300)
    calls = []
    def call(*args, **kwargs):
        at = datetime.fromisoformat(kwargs['before']) - timedelta(minutes=1)
        calls.append(at)
        return {'candles': [{**data[0][0], 'timestamp': at.isoformat()}], 'nextBefore': at.isoformat()}
    with pytest.raises(CandleDataError) as error:
        session_candles(call, '005930', start, now)
    assert len(calls) == 13 and error.value.diagnostics['paginationStop'] == 'page-limit'


def test_user_stop_interrupts_pagination_before_next_request(data):
    from tradingagents.monitoring.control import RunController, RunStopped
    control = RunController()
    rows, _, _, start, now = data
    calls = []
    def call(*args, **kwargs):
        calls.append(kwargs)
        control.cancel.set()
        return {'candles': rows[10:], 'nextBefore': rows[10]['timestamp']}
    with control.scope(), pytest.raises(RunStopped):
        session_candles(call, '005930', start, now)
    assert len(calls) == 1
