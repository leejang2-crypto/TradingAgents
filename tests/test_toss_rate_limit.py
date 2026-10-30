import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from tradingagents.brokers import toss_transport as transport
from tradingagents.brokers.toss_client import TossApiError, TossClient, TossToken, OrderWindowClosed
from tradingagents.monitoring.control import RunController, RunStopped
from tests.test_intraday import data


class Clock:
    def __init__(self):
        self.now = 100.
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def gate(monkeypatch):
    # Transport tests use tiny waits; separate gate tests verify production pacing.
    monkeypatch.setattr(transport, 'RATES', {key: 10000 for key in transport.RATES})
    monkeypatch.setattr(transport, 'GATE', transport.RequestGate())
    monkeypatch.setattr(transport, 'retry_delay', lambda headers, attempt:
                        max(.002 * (2 ** attempt), float(headers.get('Retry-After', 0))))
    return transport.GATE


async def client_for(respond, client_id='rate-test', account='fake'):
    client = TossClient(client_id, 'synthetic', account_seq=account)
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(respond))
    client._token = TossToken('synthetic', time.time() + 3600)
    return client


def test_pacing_is_shared_but_groups_and_clients_are_independent():
    clock = Clock()
    gate = transport.RequestGate(clock=clock, sleep=clock.sleep)
    async def run():
        key = ('client', 'MARKET_DATA_CHART')
        await gate.acquire(key, 200)
        await gate.acquire(('client', 'ORDER'), 200)
        await gate.acquire(('another', 'MARKET_DATA_CHART'), 200)
        assert clock.now == 100
        await gate.acquire(key, 200)
        assert clock.now == pytest.approx(100.25)
    asyncio.run(run())


def test_rate_gate_handles_concurrent_event_loops_in_threads():
    from concurrent.futures import ThreadPoolExecutor
    gate = transport.RequestGate()
    key = ('threads', 'MARKET_DATA_CHART')
    def request():
        async def run():
            await gate.acquire(key, time.monotonic() + 3)
            return time.monotonic()
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=3) as pool:
        times = sorted(pool.map(lambda _: request(), range(3)))
    assert all(b - a >= .24 for a, b in zip(times, times[1:]))


def test_server_headers_slow_all_clients_and_separate_order_group():
    clock = Clock()
    gate = transport.RequestGate(clock=clock, sleep=clock.sleep)
    key = ('client', 'MARKET_DATA_CHART')
    response = httpx.Response(200, headers={'X-RateLimit-Limit': '1',
                              'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '2'})
    rate = gate.observe(key, response)
    assert rate['retryAfterSeconds'] == 2
    async def run():
        await gate.acquire(('client', 'ORDER'), 200)
        assert clock.now == 100
        await gate.acquire(key, 200)
        assert clock.now == pytest.approx(102)
        await gate.acquire(key, 200)
        assert clock.now == pytest.approx(103.1)
    asyncio.run(run())


def test_retry_after_and_backoff_parsing(monkeypatch):
    monkeypatch.setattr(transport.random, 'uniform', lambda *a: 0)
    assert transport.retry_delay(httpx.Headers({'Retry-After': '3.5'}), 0) == 3.5
    assert transport.retry_delay(httpx.Headers({'Retry-After': 'nan', 'X-RateLimit-Reset': 'inf'}), 2) == 4
    assert transport.retry_delay(httpx.Headers({'Retry-After': '-1'}), 1) == 2
    assert transport.retry_delay(httpx.Headers({'X-RateLimit-Reset': '6'}), 0) == 6
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
    assert 8 < transport.retry_delay(httpx.Headers({'Retry-After': date}), 0) <= 10


def test_read_retries_429_then_succeeds(gate):
    attempts = []
    def respond(request):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return httpx.Response(429, headers={'Retry-After': '.02'}, json={'message': 'limit'})
        return httpx.Response(200, json={'result': {'candles': []}})
    async def run():
        async with await client_for(respond) as client:
            assert await client.get_candles('005930') == {'candles': []}
    asyncio.run(run())
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= .019


def test_repeated_429_is_bounded_and_exposes_only_safe_headers(gate):
    attempts = []
    def respond(request):
        attempts.append(request)
        return httpx.Response(429, headers={'Retry-After': '.001', 'X-RateLimit-Remaining': '0',
                              'Authorization': 'must-not-be-recorded'}, json={'message': 'limit'})
    async def run():
        async with await client_for(respond) as client:
            with pytest.raises(TossApiError) as error:
                await client.get_candles('005930')
            rate = error.value.rate_limit
            assert rate['attempts'] == 3 and rate['group'] == 'MARKET_DATA_CHART'
            assert rate['retryAt'] and 'Authorization' not in rate['headers']
    asyncio.run(run())
    assert len(attempts) == 3


@pytest.mark.parametrize('cancel', [False, True])
def test_post_orders_and_cancellations_are_never_retried(gate, cancel):
    attempts = []
    def respond(request):
        attempts.append(request.method)
        return httpx.Response(429, json={'message': 'limit'})
    async def run():
        async with await client_for(respond) as client:
            with pytest.raises(TossApiError) as error:
                if cancel: await client.cancel_order('test-order')
                else: await client.create_order({'symbol': '005930'})
            assert error.value.rate_limit['attempts'] == 1
    asyncio.run(run())
    assert attempts == ['POST']


def test_long_retry_after_fails_without_waiting_past_budget(gate):
    attempts = []
    def respond(request):
        attempts.append(True)
        return httpx.Response(429, headers={'Retry-After': '60'}, json={'message': 'limit'})
    async def run():
        async with await client_for(respond) as client:
            with transport.read_budget(.1), pytest.raises(TossApiError) as error:
                await client.get_prices(['005930'])
            assert error.value.rate_limit['retryAfterSeconds'] >= 60
    asyncio.run(run())
    assert len(attempts) == 1


def test_expired_budget_prevents_even_authentication(gate):
    async def run():
        async with await client_for(lambda r: httpx.Response(200)) as client:
            client._headers = AsyncMock()
            with transport.read_budget(0), pytest.raises(TimeoutError):
                await client.get_holdings()
            client._headers.assert_not_called()
    asyncio.run(run())


def test_slow_response_cannot_outlive_read_budget(gate):
    async def respond(request):
        await asyncio.sleep(.05)
        return httpx.Response(200, json={'result': []})
    async def run():
        async with await client_for(respond) as client:
            with transport.read_budget(.01), pytest.raises(TimeoutError):
                await client.get_holdings()
    asyncio.run(run())


def test_user_stop_during_retry_wait_prevents_second_request(gate):
    controller = RunController()
    attempts = []
    def respond(request):
        attempts.append(True)
        controller.cancel.set()
        return httpx.Response(429, json={'message': 'limit'})
    async def run():
        async with await client_for(respond) as client:
            with controller.scope(), pytest.raises(RunStopped):
                await client.get_holdings()
    asyncio.run(run())
    assert len(attempts) == 1


def test_post_deadline_is_rechecked_after_rate_wait(gate):
    async def run():
        async with await client_for(lambda r: httpx.Response(200)) as client:
            client.order_valid_until = datetime.now(timezone.utc) + timedelta(seconds=.01)
            key = (transport.namespace(client.base_url, client.client_id), 'ORDER')
            gate.observe(key, httpx.Response(429, headers={'Retry-After': '.03'}))
            client._client.post = AsyncMock()
            with pytest.raises(OrderWindowClosed):
                await client.create_order({})
            client._client.post.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('code,expected', [(401, 2), (403, 1), (500, 1)])
def test_non_429_errors_are_not_backoff_retried(gate, code, expected):
    attempts = []
    def respond(request):
        attempts.append(True)
        return httpx.Response(code, json={'message': 'failure'})
    async def run():
        async with await client_for(respond) as client:
            client._headers = AsyncMock(return_value={})
            with pytest.raises(TossApiError):
                await client.get_holdings()
    asyncio.run(run())
    assert len(attempts) == expected


def test_candidate_pipeline_recovers_from_transient_429_and_continues(gate, data):
    from tests.test_candidate_screen import Market, candidates
    from tradingagents.brokers.candidate_screen import screen_candidates
    market = Market(data)
    attempts = []
    def respond(request):
        if '/market-calendar/' in request.url.path:
            result = market.call('get_kr_calendar', data[4].date().isoformat())
        else:
            symbol = 'KOSPI' if '/market-indicators/' in request.url.path else request.url.params['symbol']
            attempts.append(symbol)
            if symbol == '005930' and attempts.count(symbol) == 1:
                return httpx.Response(429, headers={'Retry-After': '.001'}, json={'message': 'limit'})
            result = market.call('get_candles', symbol)
        return httpx.Response(200, json={'result': result})
    class Backend:
        def call(self, method, *args, **kwargs):
            async def invoke():
                async with await client_for(respond) as client:
                    return await getattr(client, method)(*args, **kwargs)
            return asyncio.run(invoke())
    result = screen_candidates(candidates(), Backend(), {'min_session_turnover': '1000000', 'buy_start_minutes': 20},
                              requested_date=data[4].date().isoformat(), clock=lambda: data[4])
    assert result['status'] == 'completed' and result['eligibleCount'] == 2
    assert result['evaluatedCount'] == 2 and result['unavailableCount'] == 0
    assert attempts == ['KOSPI', '005930', '005930', '000660']


def test_auth_429_is_not_replayed(gate):
    attempts = []
    def respond(request):
        attempts.append(request.url.path)
        return httpx.Response(429, json={'message': 'limit'})
    async def run():
        async with await client_for(respond, client_id='auth-rate-test') as client:
            client._token = None
            with pytest.raises(TossApiError) as error:
                await client.get_prices(['005930'])
            assert error.value.rate_limit['group'] == 'AUTH'
    asyncio.run(run())
    assert attempts == ['/oauth2/token']


def test_shared_cooldown_prevents_another_client_from_retrying_early(gate):
    attempts = []
    def respond(request):
        attempts.append(True)
        return httpx.Response(429, headers={'Retry-After': '60'}, json={'message': 'limit'})
    async def run():
        async with await client_for(respond) as a, await client_for(respond) as b:
            with pytest.raises(TossApiError):
                await a.get_candles('005930')
            with pytest.raises(transport.RateLimitDeferred) as error:
                await b.get_candles('000660')
            assert error.value.rate_limit['attempts'] == 0
            assert error.value.rate_limit['retryAfterSeconds'] > 59
    asyncio.run(run())
    assert len(attempts) == 1


def test_extreme_header_does_not_crash_diagnostics():
    gate = transport.RequestGate()
    rate = gate.observe(('client', 'MARKET_DATA_CHART'), httpx.Response(429, headers={
        'Retry-After': '1e100', 'X-RateLimit-Limit': '1e-100'}))
    assert rate['retryAt'] is None and rate['retryAfterSeconds'] >= 1e100
