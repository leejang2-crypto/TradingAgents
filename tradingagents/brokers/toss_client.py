from __future__ import annotations

import time
import asyncio
import hashlib
import threading
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from . import toss_transport as transport


class TossApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any | None = None) -> None:
        super().__init__(f"Toss API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload
        self.rate_limit = None


class OrderWindowClosed(RuntimeError):
    """Raised before sending an order, including after slow token acquisition."""


@dataclass
class TossToken:
    access_token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class TossClient:
    # A newly issued Toss token invalidates the previous token for the client.
    # Share it across dashboard/quote/order clients in this process.
    _tokens: dict[str, TossToken] = {}
    _token_lock = threading.Lock()
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_seq: str | None = None,
        base_url: str = "https://openapi.tossinvest.com",
        timeout: float = 10.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.base_url = base_url
        self._token: TossToken | None = None
        self.order_valid_until: datetime | None = None
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TossClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def issue_token(self) -> TossToken:
        key = (transport.namespace(self.base_url, self.client_id), "AUTH")
        deadline = min(time.monotonic() + 20, transport.READ_DEADLINE.get() or float("inf"))
        await transport.GATE.acquire(key, deadline)
        response = await self._client.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        rate = transport.GATE.observe(key, response)
        if response.status_code == 429:
            self._raise_rate_error(response, rate, 1)
        payload = self._parse_response(response, unwrap_result=False)
        token = TossToken(
            access_token=payload["access_token"],
            expires_at=time.time() + int(payload.get("expires_in", 86400)),
        )
        self._token = token
        return token

    async def get_prices(self, symbols: list[str]) -> Any:
        return await self.get("/api/v1/prices", params={"symbols": ",".join(symbols)})

    async def get_orderbook(self, symbol: str) -> Any:
        return await self.get("/api/v1/orderbook", params={"symbol": symbol})

    async def get_candles(self, symbol: str, *, before=None, index=False) -> Any:
        params = {"interval": "1m", "count": 200}
        if before:
            params["before"] = before
        if index:
            path = "/api/v1/market-indicators/" + quote(symbol, safe="") + "/candles"
        else:
            path = "/api/v1/candles"
            params.update(symbol=symbol, adjusted="false")
        return await self.get(path, params=params)

    async def get_buying_power(self, currency: str) -> Any:
        return await self.get(
            "/api/v1/buying-power",
            params={"currency": currency},
            require_account=True,
        )

    async def get_holdings(self) -> Any:
        return await self.get("/api/v1/holdings", require_account=True)

    async def get_orders(self, *, status="CLOSED", from_date=None, to_date=None, cursor=None) -> Any:
        if status not in {"OPEN", "CLOSED"}:
            raise ValueError("Invalid order status group")
        params = {"status": status, "limit": 100}
        for key, value in (("from", from_date), ("to", to_date), ("cursor", cursor)):
            if value:
                params[key] = value
        return await self.get("/api/v1/orders", params=params, require_account=True)

    async def create_order(self, order: dict[str, Any]) -> Any:
        return await self.post("/api/v1/orders", json=order, require_account=True)

    async def get_order(self, order_id: str) -> Any:
        return await self.get("/api/v1/orders/" + quote(order_id, safe=""), require_account=True)

    async def cancel_order(self, order_id: str) -> Any:
        return await self.post("/api/v1/orders/" + quote(order_id, safe="") + "/cancel", json={}, require_account=True)

    async def get_sellable_quantity(self, symbol: str) -> Any:
        return await self.get("/api/v1/sellable-quantity", params={"symbol": symbol}, require_account=True)

    async def get_kr_calendar(self, day: str) -> Any:
        return await self.get("/api/v1/market-calendar/KR", params={"date": day})

    async def get(self, path: str, params: dict[str, Any] | None = None, require_account: bool = False) -> Any:
        key = (transport.namespace(self.base_url, self.client_id), transport.api_group("GET", path))
        # Includes authentication, pacing, HTTP time and all retries, not just sleeps.
        with transport.read_budget(20):
            deadline = transport.READ_DEADLINE.get()
            retries, reauthenticated, attempts = 0, False, 0
            while True:
                transport.check_read_deadline()
                try:
                    headers = await asyncio.wait_for(self._headers(require_account=require_account),
                                                     max(.001, deadline - time.monotonic()))
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("Toss 인증 대기 제한시간 초과") from exc
                await transport.GATE.acquire(key, deadline)
                transport.check_read_deadline()
                attempts += 1
                try:
                    response = await asyncio.wait_for(self._client.get(path, params=params, headers=headers),
                                                      max(.001, deadline - time.monotonic()))
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("Toss 조회 응답 제한시간 초과") from exc
                rate = transport.GATE.observe(key, response, retries)
                transport.check_read_deadline()
                if response.status_code == 429:
                    if retries >= 2 or time.monotonic() + rate["retryAfterSeconds"] >= deadline:
                        self._raise_rate_error(response, rate, attempts)
                    retries += 1
                    continue
                if response.status_code == 401 and not reauthenticated:
                    self._tokens.pop(self._token_key(), None)
                    self._token = None
                    reauthenticated = True
                    continue
                return self._parse_response(response)

    @staticmethod
    def _raise_rate_error(response, rate, attempts):
        try:
            TossClient._parse_response(response)
        except TossApiError as exc:
            exc.rate_limit = {**rate, "attempts": attempts}
            raise

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        require_account: bool = False,
    ) -> Any:
        headers = await self._headers(require_account=require_account)
        key = (transport.namespace(self.base_url, self.client_id), transport.api_group("POST", path))
        await transport.GATE.acquire(key, time.monotonic() + 20)
        if path == "/api/v1/orders":
            from tradingagents.monitoring.control import checkpoint
            checkpoint("order-sending")
            if self.order_valid_until and datetime.now(timezone.utc) >= self.order_valid_until:
                raise OrderWindowClosed("주문 제한 시간이 지나 전송하지 않았습니다.")
        response = await self._client.post(path, json=json, headers=headers)
        rate = transport.GATE.observe(key, response)
        if response.status_code == 429:
            self._raise_rate_error(response, rate, 1)
        return self._parse_response(response)

    async def _headers(self, require_account: bool) -> dict[str, str]:
        if self._token is None or not self._token.is_valid():
            key = self._token_key()
            # Blocking lock acquisition must not block an asyncio event loop.
            acquisition = asyncio.create_task(asyncio.to_thread(self._token_lock.acquire))
            try:
                await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                await acquisition
                self._token_lock.release()
                raise
            try:
                self._token = self._tokens.get(key)
                if self._token is None or not self._token.is_valid():
                    self._tokens[key] = await self.issue_token()
            finally:
                self._token_lock.release()

        headers = {"Authorization": f"Bearer {self._token.access_token}"}
        if require_account:
            if not self.account_seq:
                raise ValueError("TOSS_ACCOUNT_SEQ is required for account APIs.")
            headers["X-Tossinvest-Account"] = self.account_seq
        return headers

    def _token_key(self):
        return hashlib.sha256(f"{self.base_url}|{self.client_id}|{self.client_secret}".encode()).hexdigest()

    @staticmethod
    def _parse_response(response: httpx.Response, unwrap_result: bool = True) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if response.status_code >= 400:
            message = payload
            if isinstance(payload, dict):
                error = payload.get("error", payload)
                if isinstance(error, dict):
                    message = error.get("message") or error.get("error_description") or error
                else:
                    message = payload.get("error_description") or error
            raise TossApiError(response.status_code, str(message), payload)

        if unwrap_result and isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload
