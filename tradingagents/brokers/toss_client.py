from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class TossApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any | None = None) -> None:
        super().__init__(f"Toss API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


@dataclass
class TossToken:
    access_token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class TossClient:
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
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TossClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def issue_token(self) -> TossToken:
        response = await self._client.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = self._parse_response(response, unwrap_result=False)
        token = TossToken(
            access_token=payload["access_token"],
            expires_at=time.time() + int(payload.get("expires_in", 86400)),
        )
        self._token = token
        return token

    async def get_prices(self, symbols: list[str]) -> Any:
        return await self.get("/api/v1/prices", params={"symbols": ",".join(symbols)})

    async def get_buying_power(self, currency: str) -> Any:
        return await self.get(
            "/api/v1/buying-power",
            params={"currency": currency},
            require_account=True,
        )

    async def create_order(self, order: dict[str, Any]) -> Any:
        return await self.post("/api/v1/orders", json=order, require_account=True)

    async def get(self, path: str, params: dict[str, Any] | None = None, require_account: bool = False) -> Any:
        headers = await self._headers(require_account=require_account)
        response = await self._client.get(path, params=params, headers=headers)
        return self._parse_response(response)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        require_account: bool = False,
    ) -> Any:
        headers = await self._headers(require_account=require_account)
        response = await self._client.post(path, json=json, headers=headers)
        return self._parse_response(response)

    async def _headers(self, require_account: bool) -> dict[str, str]:
        if self._token is None or not self._token.is_valid():
            await self.issue_token()

        headers = {"Authorization": f"Bearer {self._token.access_token}"}
        if require_account:
            if not self.account_seq:
                raise ValueError("TOSS_ACCOUNT_SEQ is required for account APIs.")
            headers["X-Tossinvest-Account"] = self.account_seq
        return headers

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
