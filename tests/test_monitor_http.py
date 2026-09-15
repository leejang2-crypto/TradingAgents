import io
import json
from unittest.mock import Mock

import pytest

import main


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        pass


def request(method, path, body=b"", *, token="test-token", length=None):
    raw = (
        f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\nX-Monitor-Token: {token}\r\n"
        f"Content-Length: {len(body) if length is None else length}\r\n\r\n"
    ).encode() + body
    sock = MemorySocket(raw)
    main.MonitorHandler(sock, ("127.0.0.1", 9999), Mock())
    headers, _, content = bytes(sock.output).partition(b"\r\n\r\n")
    return int(headers.split()[1]), content


@pytest.fixture(autouse=True)
def isolated_server(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_MONITOR_TOKEN", "test-token")
    main.initialize_trading_settings(main.make_parser().parse_args([]))


def test_settings_http_roundtrip_and_runtime_not_overwritten():
    main.write_monitor_status("running-auto-paper")
    status, body = request("POST", "/api/settings", json.dumps({
        "provider": "hybrid", "hybrid_agent_providers": {"news_analyst": "ollama"},
    }).encode())
    assert status == 200
    assert json.loads(body)["settings"]["provider"] == "hybrid"
    status, body = request("GET", "/api/status")
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot["settings"]["provider"] == "hybrid"
    assert snapshot["runtime"]["status"] == "running-auto-paper"


@pytest.mark.parametrize("length,body", [("-1", b""), ("bad", b""), ("1", b"\xff"), (None, b'{"paper_capital":"NaN"}'), (None, b'[]')])
def test_invalid_http_requests_return_400(length, body):
    status, content = request("POST", "/api/settings", body, length=length)
    assert status == 400
    assert not json.loads(content)["ok"]


def test_settings_http_rejects_wrong_token():
    status, _ = request("POST", "/api/settings", b'{"provider":"ollama"}', token="wrong")
    assert status == 401
    assert main.load_trading_settings()["provider"] == "hybrid"


def test_local_model_list_uses_only_loopback(monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(main, "urlopen", fetch)
    status, _ = request("GET", "/api/llm-models?base_url=http://example.org:11434/v1")
    assert status == 400
    fetch.assert_not_called()
    fetch.return_value = io.BytesIO(b'{"models":[{"name":"qwen3:8b"}]}')
    status, body = request("GET", "/api/llm-models")
    assert status == 200
    assert json.loads(body)["models"] == ["qwen3:8b"]


def test_page_returns_model_selection_and_no_token():
    status, body = request("GET", "/")
    assert status == 200
    assert b'name="provider"' in body
    assert b"test-token" not in body


def test_account_endpoints_require_token(monkeypatch):
    reader = Mock()
    refresher = Mock()
    monkeypatch.setattr(main, "account_view", reader)
    monkeypatch.setattr(main, "refresh_account", refresher)
    assert request("GET", "/api/account?mode=live", token="wrong")[0] == 401
    assert request("POST", "/api/account/refresh", b'{"mode":"live"}', token="wrong")[0] == 401
    reader.assert_not_called()
    refresher.assert_not_called()


def test_account_http_periods_and_invalid_parameters():
    for unit in ("day", "week", "month", "year"):
        status, body = request("GET", "/api/account?mode=dry-run&period=" + unit)
        result = json.loads(body)
        assert status == 200
        assert result["account"]["initialCapital"] == "10000000.00"
        assert result["unit"] == unit
    assert request("GET", "/api/account?mode=invalid")[0] == 503
    assert request("GET", "/api/account?period=invalid")[0] == 503


def test_account_refresh_never_changes_trading_settings(monkeypatch):
    before = main.load_trading_settings()
    refresh = Mock()
    monkeypatch.setattr(main, "refresh_account", refresh)
    assert request("POST", "/api/account/refresh", b'{"mode":"live"}')[0] == 200
    refresh.assert_called_once_with(main.LOCAL_DIR, "live", "10000000")
    assert main.load_trading_settings() == before


def test_account_assets_and_controls_are_available():
    assert request("GET", "/assets/account.js")[0] == 200
    assert request("GET", "/assets/account.css")[0] == 200
    assert request("GET", "/assets/dashboard.js")[0] == 200
    assert request("GET", "/assets/dashboard.css")[0] == 200
    assert request("GET", "/assets/../main.py")[0] == 404
    status, body = request("GET", "/")
    assert status == 200 and b'account-results' in body
    assert b'name="return_period"' in body
