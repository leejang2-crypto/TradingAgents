import io
import json
from types import SimpleNamespace

import pytest

import main
from unittest.mock import Mock
from tradingagents.monitoring import auth
from tradingagents.monitoring.control import RunController
from tradingagents.brokers.position_risk import PositionRiskManager


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


def request(server, method, path, payload=None, *, cookie=None, extra=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    headers = {"Host": "localhost", "Content-Type": "application/json",
               "X-Requested-With": "TradingAgents", "Content-Length": str(len(body)), **(extra or {})}
    if cookie:
        headers["Cookie"] = cookie
    raw = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    sock = MemorySocket(raw.encode() + body)
    main.MonitorHandler(sock, ("127.0.0.1", 9999), server)
    head, _, content = bytes(sock.output).partition(b"\r\n\r\n")
    lines = head.decode().splitlines()
    return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), content


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_MONITOR_TOKEN", "legacy-test-token")
    # Session/HTTP tests do not need to spend 128 MiB on every password comparison.
    monkeypatch.setattr(auth, "password_hash", lambda password, salt: auth.hashlib.sha256((salt + password).encode()).hexdigest())
    path = tmp_path / "config/web_login.json"
    auth.register(path, "owner", "test-password-1234")
    main.initialize_trading_settings(main.make_parser().parse_args([]))
    return SimpleNamespace(auth=auth.DashboardAuth(path), control=RunController())


def login(server):
    status, headers, _ = request(server, "POST", "/api/login", {"username": "owner", "password": "test-password-1234"})
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0]


def test_local_summary_requires_login_csrf_and_safe_path(server, tmp_path, monkeypatch):
    from tradingagents.monitoring.summary import LocalSummaryService
    server.local_summary = LocalSummaryService()
    generate = Mock(return_value={"status": "running"})
    monkeypatch.setattr(server.local_summary, "request", generate)
    directory = tmp_path / "auto_paper"
    directory.mkdir()
    (directory / "auto_paper_test.json").write_text('{"analyses":[]}')
    url = "/api/analysis-summary/local?mode=dry-run&file=auto_paper_test.json"
    assert request(server, "GET", url)[0] == 401
    assert request(server, "POST", url, {})[0] == 401
    cookie = login(server)
    assert request(server, "POST", url, {}, cookie=cookie, extra={"Origin": "http://evil.test"})[0] == 403
    assert request(server, "POST", url.replace("auto_paper_test.json", "../secret.json"), {}, cookie=cookie)[0] == 400
    generate.assert_not_called()
    assert request(server, "GET", url, cookie=cookie)[0] == 202
    assert generate.call_args.kwargs == {"start": False}
    assert request(server, "POST", url, {}, cookie=cookie)[0] == 202
    assert generate.call_args.kwargs == {"start": True}


def test_password_stored_as_salted_scrypt_hash(tmp_path):
    path = tmp_path / "login.json"
    auth.register(path, "owner", "sample-password-1234")
    data = json.loads(path.read_text())
    assert "sample-password" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert auth.password_hash("sample-password-1234", data["salt"]) == data["hash"]


def test_login_protects_all_private_reads_and_sets_cookie(server):
    status, headers, _ = request(server, "GET", "/")
    assert status == 303 and headers["Location"] == "/login"
    for path in ("/api/settings", "/api/status", "/api/account", "/api/llm-models", "/api/control", "/api/positions", "/api/research"):
        assert request(server, "GET", path)[0] == 401
    status, headers, body = request(server, "POST", "/api/login", {"username": "owner", "password": "test-password-1234"})
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert b"ta_session" not in body
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert request(server, "GET", "/api/settings", cookie=cookie)[0] == 200
    assert request(server, "POST", "/api/settings", {"provider": "openai"}, cookie=cookie)[0] == 200
    assert request(server, "GET", "/api/account", cookie=cookie)[0] == 200
    assert b'settings-token' not in request(server, "GET", "/", cookie=cookie)[2]


def test_research_is_authenticated_csrf_protected_and_separate(server, tmp_path, monkeypatch):
    from tradingagents.research.service import ResearchService
    server.research = ResearchService(tmp_path / "long_term", main.PROJECT_DIR)
    submit = Mock(return_value={"available": True})
    stop = Mock(return_value={"available": True})
    monkeypatch.setattr(server.research, "submit", submit)
    monkeypatch.setattr(server.research, "stop", stop)
    payload = {"requestId": "a" * 32}
    assert request(server, "POST", "/api/research/run", payload)[0] == 401
    cookie = login(server)
    assert request(server, "POST", "/api/research/run", payload, cookie=cookie, extra={"Origin": "https://evil.example"})[0] == 403
    submit.assert_not_called()
    original = server.control.snapshot()
    settings = main.load_trading_settings()
    assert request(server, "POST", "/api/research/run", payload, cookie=cookie)[0] == 202
    submit.assert_called_once_with(payload, settings)
    assert server.control.snapshot() == original
    assert main.load_trading_settings() == settings
    assert request(server, "POST", "/api/research/stop", payload, cookie=cookie)[0] == 202
    stop.assert_called_once_with(payload["requestId"])
    assert server.control.snapshot() == original
    server.control.active = {"id": "scheduled-job", "phase": "analysis"}
    assert request(server, "POST", "/api/research/run", payload, cookie=cookie)[0] == 409
    assert submit.call_count == 1 and not server.control.cancel.is_set()


def test_universe_upload_requires_auth_csrf_and_valid_csv(server, tmp_path):
    from tradingagents.research.service import ResearchService
    from tests.test_research_universe import csv_payload
    server.research = ResearchService(tmp_path / "long_term", main.PROJECT_DIR)
    path, payload = "/api/research/universe", csv_payload()
    payload.update(fileName="kospi200.csv", asOf="2025-01-01")
    assert request(server, "POST", path, payload)[0] == 401
    cookie = login(server)
    assert request(server, "POST", path, payload, cookie=cookie, extra={"Origin": "https://evil.example"})[0] == 403
    original, settings = server.control.snapshot(), main.load_trading_settings()
    assert request(server, "POST", path, {**payload, "confirmed": False}, cookie=cookie)[0] == 400
    status, _, body = request(server, "POST", path, payload, cookie=cookie)
    assert status == 202 and len(json.loads(body)["universe"]["members"]) == 200
    uploaded = json.loads(body)["universe"]
    assert uploaded["fileName"] == "kospi200.csv" and uploaded["asOf"] == "2025-01-01"
    assert uploaded["registeredAt"].endswith("+09:00")
    assert request(server, "POST", path, {**payload, "csv": "x" * 150001}, cookie=cookie)[0] == 413
    from tests.krx_english_sample import english_sample
    payload["csv"] = english_sample()
    status, _, body = request(server, "POST", path, payload, cookie=cookie)
    assert status == 400 and "201종목 원본 확인" in json.loads(body)["error"]
    assert len(server.research.snapshot()["universe"]["members"]) == 200
    payload["nonstandardCountConfirmed"] = True
    status, _, body = request(server, "POST", path, payload, cookie=cookie)
    snapshot = json.loads(body)["universe"]
    assert status == 202 and snapshot["importInfo"]["rowCount"] == 201
    assert snapshot["members"]["0126Z0.KS"] == "SAMSUNG EPIS HOLDINGS"
    assert server.control.snapshot() == original and main.load_trading_settings() == settings


def test_logout_and_password_reset_invalidate_sessions(server):
    cookie = login(server)
    assert request(server, "POST", "/api/logout", {}, cookie=cookie)[0] == 200
    assert request(server, "GET", "/api/status", cookie=cookie)[0] == 401
    cookie = login(server)
    auth.register(server.auth.path, "owner", "new-password-1234")
    assert request(server, "GET", "/api/status", cookie=cookie)[0] == 401


@pytest.mark.parametrize("extra", [
    {"X-Requested-With": ""}, {"Origin": "https://evil.invalid"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_cookie_writes_and_login_reject_cross_site_requests(server, extra):
    cookie = login(server)
    assert request(server, "POST", "/api/settings", {"provider": "ollama"}, cookie=cookie, extra=extra)[0] == 403
    assert request(server, "POST", "/api/login", {}, extra=extra)[0] == 403


def test_login_rate_limit_and_missing_registration(server):
    for _ in range(5):
        assert request(server, "POST", "/api/login", {"username": "owner", "password": "wrong"})[0] == 401
    assert request(server, "POST", "/api/login", {"username": "owner", "password": "test-password-1234"})[0] == 429
    server.auth.attempts.clear()
    server.auth.path.unlink()
    assert request(server, "POST", "/api/login", {})[0] == 503


def test_session_expiry_restart_and_secure_cookie(server, monkeypatch):
    cookie = login(server)
    stamp = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: stamp + auth.IDLE_SECONDS + 1)
    assert request(server, "GET", "/api/status", cookie=cookie)[0] == 401
    fresh = auth.DashboardAuth(server.auth.path, secure_cookie=True)
    assert not fresh.authorized({"Cookie": cookie})
    assert "; Secure" in fresh.cookie("example")


def test_manual_endpoints_auth_and_busy_resolution(server, monkeypatch):
    command = {"requestId": "1" * 32, "action": "news", "ticker": "005930.KS"}
    assert request(server, "POST", "/api/control/run", command)[0] == 401
    cookie = login(server)
    assert request(server, "POST", "/api/control/run", command, cookie=cookie)[0] == 202
    server.control.start()
    command["requestId"] = "2" * 32
    status, _, body = request(server, "POST", "/api/control/run", command, cookie=cookie)
    assert status == 409
    active = json.loads(body)["control"]["active"]
    command.update(policy="stop", activeId=active["id"])
    assert request(server, "POST", "/api/control/run", command, cookie=cookie)[0] == 202
    assert server.control.cancel.is_set()
    assert request(server, "POST", "/api/control/pause", {"paused": True}, cookie=cookie)[0] == 202
    assert server.control.paused
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: False)
    command.update(requestId="3" * 32, action="trade")
    assert request(server, "POST", "/api/control/run", command, cookie=cookie)[0] == 400


def test_position_management_writes_use_login_and_csrf_guard(server):
    server.risk = Mock(spec=PositionRiskManager)
    server.risk.snapshot.return_value = {"enabled": True, "mode": "live", "positions": {}}
    payload = {"symbol": "005930", "cycle": "cycle", "allowed": True}
    assert request(server, "POST", "/api/positions/carry", payload)[0] == 401
    server.risk.carry.assert_not_called()
    cookie = login(server)
    assert request(server, "GET", "/api/positions", cookie=cookie)[0] == 200
    assert request(server, "POST", "/api/positions/carry", payload, cookie=cookie, extra={"Origin": "https://evil.invalid"})[0] == 403
    assert request(server, "POST", "/api/positions/carry", payload, cookie=cookie)[0] == 200
    server.risk.carry.assert_called_once_with("005930", "cycle", True)
    assert request(server, "POST", "/api/positions/adopt", {"symbol": "005930", "acquiredDate": "2026-09-14", "confirmed": True}, cookie=cookie)[0] == 200
    server.risk.adopt.assert_called_once_with("005930", "2026-09-14", True)


@pytest.mark.parametrize("settings", [
    {"position_check_seconds": 0}, {"position_check_seconds": 301}, {"liquidation_minutes": 0},
    {"buy_cutoff_minutes": 10, "liquidation_minutes": 15}, {"liquidation_minutes": True},
])
def test_invalid_risk_settings_are_rejected(server, settings):
    assert request(server, "POST", "/api/settings", settings, cookie=login(server))[0] == 400
