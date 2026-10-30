from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock
import time

import pytest

from tests.test_intraday import data
from tradingagents.brokers import intraday
from tradingagents.brokers.intraday import session_candles, CandleDataError
from tradingagents.brokers.toss_transport import read_budget


@pytest.fixture(autouse=True)
def clean_cache():
    intraday._SESSION_CACHE.clear()
    yield
    intraday._SESSION_CACHE.clear()


def test_completed_snapshot_is_reused_and_mutation_is_isolated(data):
    rows, _, _, start, now = data
    call = Mock(return_value={'candles': rows})
    first = session_candles(call, '005930', start, now, cache_namespace='client')
    first[0]['closePrice'] = '1'
    info = {}
    second = session_candles(call, '005930', start, now + timedelta(seconds=10),
                             cache_namespace='client', diagnostics=info)
    assert call.call_count == 1 and second == rows
    assert info['cacheHit'] and info['cacheAgeSeconds'] >= 0
    assert info['asOf'] == (now + timedelta(seconds=10)).isoformat()


@pytest.mark.parametrize('change', ['client', 'symbol', 'day', 'boundary', 'expired'])
def test_different_contexts_and_stale_cache_are_not_reused(data, change):
    rows, _, _, start, now = deepcopy(data)
    call = Mock(return_value={'candles': rows})
    session_candles(call, '005930', start, now, cache_namespace='client')
    namespace, symbol = 'client', '005930'
    if change == 'client': namespace = 'different'
    if change == 'symbol': symbol = '000660'
    if change == 'day':
        start += timedelta(days=1)
        now += timedelta(days=1)
        for row in rows:
            row['timestamp'] = (intraday.stamp(row['timestamp']) + timedelta(days=1)).isoformat()
    if change == 'boundary':
        now += timedelta(minutes=5)
        rows.extend({**rows[-1], 'timestamp': (start + timedelta(minutes=i)).isoformat()} for i in range(21, 26))
    if change == 'expired':
        key = next(iter(intraday._SESSION_CACHE))
        entry = intraday._SESSION_CACHE[key]
        intraday._SESSION_CACHE[key] = (time.monotonic() - 31, *entry[1:])
    session_candles(call, symbol, start, now, cache_namespace=namespace)
    assert call.call_count == 2


@pytest.mark.parametrize('kind', ['missing', 'ohlc'])
def test_bad_data_is_never_cached(data, kind):
    rows, _, _, start, now = deepcopy(data)
    if kind == 'missing': rows.pop(4)
    else: rows[4]['openPrice'] = 'NaN'
    call = Mock(return_value={'candles': rows})
    with pytest.raises(ValueError):
        session_candles(call, '005930', start, now, cache_namespace='client')
    assert not intraday._SESSION_CACHE
    call.return_value = {'candles': data[0]}
    assert len(session_candles(call, '005930', start, now, cache_namespace='client')) == 20


def test_index_opening_exception_is_cached_with_warning_but_not_for_stock(data):
    rows, _, _, start, now = data
    raw = [{**r, 'timestamp': (intraday.stamp(r['timestamp']) - timedelta(minutes=1)).isoformat()}
           for r in rows[1:]]
    call = Mock(return_value={'candles': raw})
    session_candles(call, 'KOSPI', start, now, index=True, cache_namespace='client')
    info = {}
    session_candles(call, 'KOSPI', start, now, index=True, cache_namespace='client', diagnostics=info)
    assert call.call_count == 2 and info['cacheHit'] and info['warning']
    assert info['missingCount'] == 1
    call.return_value = {'candles': rows[1:]}
    with pytest.raises(CandleDataError):
        session_candles(call, '005930', start, now, cache_namespace='client')


def test_expired_deadline_does_not_allow_cached_data(data):
    rows, _, _, start, now = data
    call = Mock(return_value={'candles': rows})
    session_candles(call, '005930', start, now, cache_namespace='client')
    with read_budget(0), pytest.raises(TimeoutError):
        session_candles(call, '005930', start, now, cache_namespace='client')
    assert call.call_count == 1


def test_final_entry_reuses_charts_but_fetches_fresh_orderbook(data, tmp_path, monkeypatch):
    from tradingagents.brokers.position_risk import BrokerBackend
    rows, book, _, start, now = data
    monkeypatch.setattr('tradingagents.brokers.position_risk.credentials', lambda: ('cache-test', 'synthetic'))
    backend = BrokerBackend(tmp_path, False, 10000000, clock=lambda: now)
    def call(method, symbol, **kwargs):
        if method == 'get_orderbook': return deepcopy(book)
        result = deepcopy(rows)
        if kwargs.get('index'):
            for row in result:
                row['timestamp'] = (intraday.stamp(row['timestamp']) - timedelta(minutes=1)).isoformat()
        return {'candles': result}
    backend.call = Mock(side_effect=call)
    first = backend.entry_signal('005930', start, now, {'min_session_turnover': '1000000'})
    second = backend.entry_signal('005930', start, now, {'min_session_turnover': '1000000'})
    assert first == second and first['allowed']
    assert sum(c.args[0] == 'get_candles' for c in backend.call.call_args_list) == 3
    assert sum(c.args[0] == 'get_orderbook' for c in backend.call.call_args_list) == 2
