"""Process-wide pacing for Toss clients, including clients on different event loops."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
import random
import threading
import time


READ_DEADLINE = ContextVar("toss_read_deadline", default=None)


class RateLimitDeferred(TimeoutError):
    status_code = 429

    def __init__(self, rate_limit):
        super().__init__("Toss API 공통 대기 시간이 조회 예산을 초과하여 요청하지 않았습니다.")
        self.rate_limit = rate_limit


def retry_at(seconds):
    try:
        return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None

# Conservative local ceilings, not claims about the provider's current quota.
# Lower server limits and shared cooldowns always take precedence.
RATES = {"AUTH": 1, "ACCOUNT": 1, "ASSET": 2, "MARKET_INFO": 2,
         "MARKET_DATA": 5, "MARKET_DATA_CHART": 4, "MARKET_INDICATOR_CHART": 2,
         "ORDER": 5, "ORDER_HISTORY": 2, "ORDER_INFO": 2, "OTHER": 1}


def namespace(base_url, client_id):
    return hashlib.sha256(f"{base_url.rstrip('/')}|{client_id}".encode()).hexdigest()


def api_group(method, path):
    if path == "/oauth2/token": return "AUTH"
    if path == "/api/v1/candles": return "MARKET_DATA_CHART"
    if path.startswith("/api/v1/market-indicators/") and path.endswith("/candles"):
        return "MARKET_INDICATOR_CHART"
    if path in {"/api/v1/prices", "/api/v1/orderbook", "/api/v1/trades", "/api/v1/price-limits"}:
        return "MARKET_DATA"
    if path.startswith("/api/v1/market-calendar/"): return "MARKET_INFO"
    if path == "/api/v1/accounts": return "ACCOUNT"
    if path == "/api/v1/holdings": return "ASSET"
    if path in {"/api/v1/buying-power", "/api/v1/sellable-quantity", "/api/v1/commissions"}:
        return "ORDER_INFO"
    if path == "/api/v1/orders" or path.startswith("/api/v1/orders/"):
        return "ORDER_HISTORY" if method == "GET" else "ORDER"
    return "OTHER"


def nonnegative(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def retry_delay(headers, attempt):
    delay = nonnegative(headers.get("Retry-After"))
    if delay is None and headers.get("Retry-After"):
        try:
            at = parsedate_to_datetime(headers["Retry-After"])
            delay = max(0, at.timestamp() - time.time()) if at.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            pass
    reset = nonnegative(headers.get("X-RateLimit-Reset")) or 0
    return max(delay or 0, reset, 2 ** min(attempt, 4)) + random.uniform(0, .25)


@contextmanager
def read_budget(seconds):
    deadline = time.monotonic() + max(0, seconds)
    parent = READ_DEADLINE.get()
    token = READ_DEADLINE.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
    finally:
        READ_DEADLINE.reset(token)


def check_read_deadline():
    from tradingagents.monitoring.control import checkpoint
    checkpoint("toss-read")
    deadline = READ_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Toss 조회 제한시간 또는 매수 가능 시간 만료")


class RequestGate:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.states = {}

    def _state(self, key):
        return self.states.setdefault(key, {"next": 0, "cooldown": 0, "interval": 1 / RATES[key[1]]})

    async def acquire(self, key, deadline):
        from tradingagents.monitoring.control import checkpoint
        while True:
            checkpoint("toss-rate-limit-wait")
            now = self.clock()
            if now >= deadline:
                raise TimeoutError("Toss 요청 대기 제한시간 초과")
            with self.lock:
                state = self._state(key)
                wait = max(state["next"], state["cooldown"]) - now
                if state["cooldown"] >= deadline and state.get("rateLimit"):
                    raise RateLimitDeferred({**state["rateLimit"], "attempts": 0,
                                             "retryAfterSeconds": wait, "retryAt": retry_at(wait)})
                if wait <= 0:
                    state["next"] = now + state["interval"]
                    return
            # Do not hold a thread lock or an order/account group while awaiting charts.
            await self.sleep(min(wait, .25, deadline - now))

    def observe(self, key, response, attempt=0):
        headers = response.headers
        now = self.clock()
        delay = retry_delay(headers, attempt) if response.status_code == 429 else 0
        with self.lock:
            state = self._state(key)
            limit = nonnegative(headers.get("X-RateLimit-Limit"))
            if limit is not None and limit >= 1:
                state["interval"] = max(state["interval"], 1.1 / limit)
                state["next"] = max(state["next"], now + state["interval"])
            remaining = nonnegative(headers.get("X-RateLimit-Remaining"))
            if remaining is not None and remaining <= 1:
                delay = max(delay, nonnegative(headers.get("X-RateLimit-Reset")) or state["interval"])
            if delay:
                state["cooldown"] = max(state["cooldown"], now + delay)
            wait = max(0, state["cooldown"] - now)
            rate = {"group": key[1], "retryAfterSeconds": wait, "retryAt": retry_at(wait),
                    "headers": {name: headers[name][:128] for name in (
                        "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset") if name in headers}}
            if response.status_code == 429:
                state["rateLimit"] = rate
        return rate


GATE = RequestGate()
