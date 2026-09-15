import asyncio
from unittest.mock import Mock

import pytest

from tradingagents.brokers import account_service as service
from tradingagents.brokers import toss_executor
from tradingagents.brokers.paper_account import PaperAccount, now_kst
from tradingagents.brokers.toss_client import TossClient, TossToken


@pytest.fixture
def fake_toss(monkeypatch):
    calls = []
    class FakeToss:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get_prices(self, symbols):
            calls.append("prices")
            return [{"symbol": symbol, "lastPrice": "70000", "currency": "KRW", "timestamp": now_kst().isoformat()} for symbol in symbols]
        async def get_holdings(self):
            calls.append("holdings")
            return {"items": [], "dailyProfitLoss": {"rate": "0.01"}}
        async def get_buying_power(self, currency):
            calls.append(currency)
            return {"currency": currency, "cashBuyingPower": "10000"}
        async def get_orders(self, **kwargs):
            calls.append(kwargs["status"])
            return {"orders": [], "hasNext": kwargs["status"] == "CLOSED"}
        async def create_order(self, *args, **kwargs):
            raise AssertionError("Account reads must never trade")
    monkeypatch.setattr(service, "TossClient", FakeToss)
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"):
        monkeypatch.setenv(name, "test-only")
    return calls


def test_live_reads_supported_fields_without_inventing_period_returns(tmp_path, fake_toss):
    service.refresh_account(tmp_path, "live")
    for unit in ("day", "week", "month", "year"):
        result = service.account_view(tmp_path, "live", unit)
        assert result["returns"] == []
        assert result["dailyHoldingsReturn"]["rate"] == "0.01"
        assert result["account"]["buyingPower"]["KRW"]["cashBuyingPower"] == "10000"
        assert result["account"]["ordersTruncated"]
    service.refresh_account(tmp_path, "live")
    assert fake_toss.count("holdings") == 1


def test_mode_and_account_cache_isolation(tmp_path, fake_toss, monkeypatch):
    service.refresh_account(tmp_path, "live")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "another")
    assert service.account_view(tmp_path, "live")["account"] is None
    paper = service.account_view(tmp_path, "dry-run")
    assert paper["account"]["cash"] == "10000000.00"


def test_main_paper_execution_updates_ledger_without_real_orders(tmp_path, fake_toss, monkeypatch):
    monkeypatch.setattr(toss_executor, "TossClient", Mock(side_effect=AssertionError("No real order")))
    plan = toss_executor.plan_from_decision("**Rating**: Buy", "005930.KS")
    result = asyncio.run(toss_executor.execute_plan(plan,
        paper_account_path=tmp_path / "paper.sqlite3", paper_trade_log=tmp_path / "trades.jsonl", experience_run_id="test"))
    assert not result["executed"]
    assert result["paperTrade"]["record"]["status"] == "SIMULATED_FILLED"
    assert PaperAccount(tmp_path / "paper.sqlite3").symbols() == ["005930"]


def test_toss_methods_use_read_only_endpoints(monkeypatch):
    calls = []
    async def get(path, **kwargs):
        calls.append((path, kwargs))
        return {}
    async def check():
        async with TossClient("test-method", "secret") as client:
            monkeypatch.setattr(client, "get", get)
            await client.get_holdings()
            await client.get_orders(from_date="2026-09-01", to_date="2026-09-11")
    asyncio.run(check())
    assert calls[0] == ("/api/v1/holdings", {"require_account": True})
    assert calls[1][1]["params"]["from"] == "2026-09-01"


def test_toss_clients_share_token_in_one_process(monkeypatch):
    issued = []
    async def issue(client):
        import time
        issued.append(True)
        client._token = TossToken("test-token", time.time() + 3600)
        return client._token
    monkeypatch.setattr(TossClient, "issue_token", issue)
    async def check():
        async with TossClient("shared-token-test", "secret") as a, TossClient("shared-token-test", "secret") as b:
            await a._headers(False)
            await b._headers(False)
    asyncio.run(check())
    assert len(issued) == 1


def test_read_reauthenticates_once_but_order_post_never_retries(monkeypatch):
    import httpx
    import time
    from tradingagents.brokers.toss_client import TossApiError
    attempts = []
    async def issue(client):
        client._token = TossToken("test", time.time() + 3600)
        return client._token
    def respond(request):
        attempts.append(request.method)
        return httpx.Response(401 if len(attempts) == 1 or request.method == "POST" else 200, json={"result": []})
    monkeypatch.setattr(TossClient, "issue_token", issue)
    async def check():
        async with TossClient("reauth-test", "secret", account_seq="fake") as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(respond))
            assert await client.get_holdings() == []
            with pytest.raises(TossApiError):
                await client.create_order({})
    asyncio.run(check())
    assert attempts == ["GET", "GET", "POST"]
