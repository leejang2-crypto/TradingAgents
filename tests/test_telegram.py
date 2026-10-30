import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock

from dotenv import dotenv_values
import pytest

import main
from tradingagents.monitoring import telegram as tg, telegram_setup as setup
from tests.test_position_risk import env, buy
from tests.test_dashboard_auth import server, request, login


TOKEN = "123456:" + "a" * 32
CHAT = "123456789"
ENV = {tg.PREFIX + "BOT_TOKEN": TOKEN, tg.PREFIX + "CHAT_ID": CHAT, tg.PREFIX + "ENABLED": "1"}


@pytest.fixture
def service(tmp_path):
    now, sent, settings = [1700000000.0], [], {}
    notifier = tg.TelegramNotifier(tmp_path, "dry-run", lambda: settings, environ=ENV,
                                   sender=sent.append, clock=lambda: now[0])
    return notifier, now, sent, settings


def rows(notifier):
    with notifier._db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM alerts ORDER BY created,rowid")]


def test_missing_credentials_are_opt_in_and_no_io(tmp_path):
    sender = Mock()
    notifier = tg.TelegramNotifier(tmp_path, "dry-run", lambda: {}, environ={}, sender=sender)
    assert not notifier.emit("system", "start")
    assert not notifier.test()
    assert not notifier.path.exists()
    assert not notifier.status()["configured"]
    sender.assert_not_called()


@pytest.mark.parametrize("key,value", [("BOT_TOKEN", "bad"), ("CHAT_ID", "@someone"), ("ENABLED", "0")])
def test_invalid_or_disabled_credentials_never_send(tmp_path, key, value):
    service = tg.TelegramNotifier(tmp_path, "live", lambda: {}, environ={**ENV, tg.PREFIX + key: value})
    assert not service.emit("orders", "fill")
    assert not service.path.exists()


def test_queue_dedup_redaction_permissions_and_status(service):
    notifier, now, sent, _ = service
    for _ in range(2):
        assert notifier.emit("orders", "체결", [TOKEN, "sk-private-key"], key="fill-1")
    assert not sent
    assert len(rows(notifier)) == 1
    assert notifier.path.stat().st_mode & 0o777 == 0o600
    assert notifier.deliver_one()
    assert len(sent) == 1 and "모의투자" in sent[0]
    assert TOKEN not in sent[0] and "sk-private-key" not in sent[0]
    assert notifier.status()["recent"][0]["status"] == "sent"
    public = json.dumps(notifier.status())
    assert TOKEN not in public and CHAT not in public
    restarted = tg.TelegramNotifier(notifier.path.parents[1], "dry-run", lambda: {},
                                    environ=ENV, sender=sent.append, clock=lambda: now[0])
    assert restarted.emit("orders", "체결", key="fill-1")
    assert not restarted.deliver_one()
    assert len(sent) == 1


def test_modes_and_destination_are_isolated(service):
    notifier, now, sent, _ = service
    notifier.emit("system", "dry", key="same")
    live = tg.TelegramNotifier(notifier.path.parents[1], "live", lambda: {},
                              environ=ENV, sender=sent.append, clock=lambda: now[0])
    live.emit("system", "live", key="same")
    live.deliver_one()
    assert len(sent) == 1 and "실투자" in sent[0]
    assert notifier.status()["pending"] == 1
    moved = tg.TelegramNotifier(notifier.path.parents[1], "dry-run", lambda: {},
        environ={**ENV, tg.PREFIX + "CHAT_ID": "987654321"}, sender=sent.append, clock=lambda: now[0])
    assert not moved.deliver_one()
    assert rows(notifier)[0]["status"] == "cancelled"


def test_category_toggle_cancels_pending_and_test_is_throttled(service):
    notifier, now, sent, settings = service
    notifier.emit("analysis", "analysis")
    settings["telegram_analysis"] = False
    assert not notifier.deliver_one()
    assert rows(notifier)[0]["status"] == "cancelled"
    assert not notifier.emit("analysis", "again")
    assert notifier.test() and not notifier.test()
    now[0] += 30
    assert notifier.test()
    settings["telegram_enabled"] = False
    assert not notifier.emit("errors", "error")
    assert not notifier.deliver_one()
    assert not sent


def test_expiration_bounded_queue_and_retention(service):
    notifier, now, sent, _ = service
    notifier.emit("system", "old")
    now[0] += 3601
    assert not notifier.deliver_one()
    assert rows(notifier)[0]["status"] == "expired"
    now[0] += 7 * 86400
    notifier.deliver_one()
    assert not rows(notifier)
    with notifier._db() as db:
        db.executemany("INSERT INTO alerts(id,status) VALUES(?,'pending')", [(str(n),) for n in range(500)])
    assert not notifier.emit("system", "overflow")
    assert not sent


def test_disk_failure_does_not_escape_into_trading(service, monkeypatch):
    notifier, _, sent, _ = service
    monkeypatch.setattr(notifier, "_db", Mock(side_effect=OSError(TOKEN)))
    assert not notifier.emit("orders", "test")
    assert TOKEN not in json.dumps(notifier.status())
    assert not sent


def test_send_pacing_even_when_producers_wake_worker(service):
    notifier, now, sent, _ = service
    notifier.emit("system", "one")
    notifier.emit("system", "two")
    assert notifier.deliver_one()
    assert not notifier.deliver_one()
    now[0] += 1.2
    assert notifier.deliver_one()
    assert len(sent) == 2


def test_rate_limit_waits_retry_after_without_trading_side_effects(service):
    notifier, now, sent, _ = service
    notifier.sender = Mock(side_effect=[tg.TelegramError(429, 90), None])
    notifier.emit("orders", "test")
    notifier.deliver_one()
    assert rows(notifier)[0]["status"] == "pending"
    now[0] += 89
    assert not notifier.deliver_one()
    now[0] += 1
    assert notifier.deliver_one()
    assert rows(notifier)[0]["status"] == "sent"
    assert notifier.sender.call_count == 2


@pytest.mark.parametrize("error", [TimeoutError(TOKEN), tg.TelegramError(503)])
def test_transient_errors_stop_at_four_attempts_and_hide_details(service, error):
    notifier, now, _, _ = service
    notifier.sender = Mock(side_effect=error)
    notifier.emit("system", "start")
    for _ in range(4):
        notifier.deliver_one()
        now[0] += 100
    assert rows(notifier)[0]["status"] == "failed"
    assert rows(notifier)[0]["attempts"] == 4
    assert not notifier.deliver_one()
    assert TOKEN not in json.dumps(notifier.status())


@pytest.mark.parametrize("code", [400, 401, 403, "tls-certificate"])
def test_permanent_api_error_is_not_retried(service, code):
    notifier, now, _, _ = service
    notifier.sender = Mock(side_effect=tg.TelegramError(code))
    notifier.emit("system", "start")
    assert notifier.deliver_one()
    now[0] += 301
    assert not notifier.deliver_one()
    assert rows(notifier)[0]["status"] == "failed"
    assert notifier.sender.call_count == 1


def connection(body, status=200):
    result = Mock()
    result.getresponse.return_value.status = status
    result.getresponse.return_value.read.return_value = json.dumps(body).encode()
    return result


def test_transport_only_posts_https_and_validates_message_receipt(monkeypatch):
    conn = connection({"ok": True, "result": {"message_id": 4}})
    factory = Mock(return_value=conn)
    context_factory = Mock(wraps=tg.ssl.create_default_context)
    monkeypatch.setattr(tg.ssl, "create_default_context", context_factory)
    assert tg.api_call(TOKEN, "sendMessage", {"text": "test"}, connection_factory=factory) == {"message_id": 4}
    context_factory.assert_called_once_with(cafile=tg.certifi.where())
    context = factory.call_args.kwargs["context"]
    factory.assert_called_once_with("api.telegram.org", timeout=5, context=context)
    assert context.verify_mode == tg.ssl.CERT_REQUIRED and context.check_hostname
    assert conn.request.call_args.args[:2] == ("POST", f"/bot{TOKEN}/sendMessage")
    conn.close.assert_called_once()


@pytest.mark.parametrize("body,status", [
    ({"ok": True}, 200), ({"ok": True, "result": {}}, 200),
    ({"ok": False, "description": TOKEN, "parameters": "bad"}, 500),
    ({"ok": False, "description": TOKEN, "error_code": TOKEN}, 400),
    ({"ok": True, "result": {}}, 302), ([], 200),
])
def test_transport_errors_never_include_token_or_follow_redirect(body, status):
    conn = connection(body, status)
    with pytest.raises(tg.TelegramError) as error:
        tg.api_call(TOKEN, "sendMessage", {}, connection_factory=lambda *a, **k: conn)
    assert TOKEN not in str(error.value)
    assert conn.request.call_count == 1
    conn.close.assert_called_once()


def test_transport_timeout_and_retry_after_are_sanitized():
    conn = connection({"ok": False, "error_code": 429, "parameters": {"retry_after": 42}}, 429)
    with pytest.raises(tg.TelegramError) as error:
        tg.api_call(TOKEN, "sendMessage", {}, connection_factory=lambda *a, **k: conn)
    assert error.value.code == 429 and error.value.retry_after == 42
    conn.request.side_effect = TimeoutError(TOKEN)
    with pytest.raises(tg.TelegramError, match="network-or-response") as error:
        tg.api_call(TOKEN, "sendMessage", {}, connection_factory=lambda *a, **k: conn)
    assert TOKEN not in str(error.value)


def test_tls_failure_is_distinct_and_never_disables_verification():
    conn = connection({})
    conn.request.side_effect = tg.ssl.SSLCertVerificationError(1, TOKEN)
    with pytest.raises(tg.TelegramError, match="tls-certificate") as error:
        tg.api_call(TOKEN, "sendMessage", {}, connection_factory=lambda *a, **k: conn)
    assert TOKEN not in str(error.value)
    assert conn.request.call_count == 1
    conn.close.assert_called_once()


def ledger_notifier(manager, clock):
    return tg.TelegramNotifier(manager.local_dir, "live", lambda: {}, risk_path=manager.store.path,
                              environ=ENV, sender=Mock(), clock=lambda: clock[0].timestamp())


def test_notifications_only_use_committed_verified_fills(env):
    manager, broker, clock, _ = env
    notifier = ledger_notifier(manager, clock)
    buy(manager)
    notifier.observe_positions()
    manager.tick()
    before = manager.store.path.read_bytes()
    notifier.observe_positions()
    notifier.observe_positions()
    messages = rows(notifier)
    assert len(messages) == 1
    assert "전량 체결 확인" in messages[0]["text"] and "10주" in messages[0]["text"]
    assert manager.store.path.read_bytes() == before
    assert len(broker.orders) == 1


def test_partial_fill_then_completion_and_restart_dedup(env):
    manager, broker, clock, _ = env
    broker.auto_fill = False
    notifier = ledger_notifier(manager, clock)
    buy(manager)
    notifier.observe_positions()
    assert "체결 완료 아님" in rows(notifier)[0]["text"]
    broker.fill("broker-1", 4, "PARTIAL_FILLED")
    manager.tick()
    notifier.observe_positions()
    assert "부분 체결" in rows(notifier)[-1]["text"] and "4주" in rows(notifier)[-1]["text"]
    clock[0] += timedelta(seconds=5)
    restarted = ledger_notifier(manager, clock)
    restarted.observe_positions()
    assert len(rows(notifier)) == 2
    broker.fill("broker-1", 10, "FILLED")
    manager.tick()
    restarted.observe_positions()
    assert len(rows(notifier)) == 3 and "전량 체결 확인" in rows(notifier)[-1]["text"]


def test_historical_fills_are_baselined_but_new_sell_is_reported(env):
    manager, broker, clock, _ = env
    buy(manager)
    manager.tick()
    clock[0] += timedelta(minutes=1)
    notifier = ledger_notifier(manager, clock)
    notifier.observe_positions()
    assert not rows(notifier)
    broker.price += 1
    manager.tick()
    notifier.observe_positions()
    assert any("청산/보유 관리" in row["text"] for row in rows(notifier))
    manager.tick()
    notifier.observe_positions()
    assert any("SELL" in row["text"] and "전량 체결" in row["text"] for row in rows(notifier))


def test_uncertain_order_does_not_claim_fill_or_resubmit(env):
    manager, broker, clock, _ = env
    broker.fail_submit = True
    notifier = ledger_notifier(manager, clock)
    with pytest.raises(ValueError, match="접수 결과 불확실"):
        buy(manager)
    notifier.observe_positions()
    assert rows(notifier)[0]["category"] == "errors"
    assert "UNKNOWN" in rows(notifier)[0]["text"] and "체결 완료 아님" in rows(notifier)[0]["text"]
    assert len(broker.orders) == 1


def test_invalid_fill_is_not_reported_as_verified(env):
    manager, broker, clock, _ = env
    broker.auto_fill = False
    notifier = ledger_notifier(manager, clock)
    buy(manager)
    notifier.observe_positions()
    broker.fill("broker-1", 4, "FILLED")
    manager.tick()
    notifier.observe_positions()
    assert not any("전량 체결 확인" in row["text"] or "4주" in row["text"] for row in rows(notifier))


def test_shutdown_observes_final_fill_without_network(env):
    manager, _, clock, _ = env
    notifier = ledger_notifier(manager, clock)
    buy(manager)
    manager.tick()
    notifier.stop()
    assert any("전량 체결 확인" in row["text"] for row in rows(notifier))
    notifier.sender.assert_not_called()


def test_save_credentials_preserves_shared_env_and_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# shared settings\nOPENAI_API_KEY='keep-me'\nTOSS_CLIENT_ID=keep-too\n"
                    + tg.PREFIX + "BOT_TOKEN='old'\n", encoding="utf-8")
    setup.save_credentials(path, TOKEN, CHAT)
    saved = dotenv_values(path)
    assert saved["OPENAI_API_KEY"] == "keep-me" and saved["TOSS_CLIENT_ID"] == "keep-too"
    assert saved[tg.PREFIX + "BOT_TOKEN"] == TOKEN and saved[tg.PREFIX + "CHAT_ID"] == CHAT
    assert saved[tg.PREFIX + "ENABLED"] == "1"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "# shared settings" in path.read_text()
    assert len(list(tmp_path.glob('.telegram-env-*'))) == 0


def test_env_replace_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OTHER=keep\n")
    monkeypatch.setattr(setup.os, "replace", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        setup.save_credentials(path, TOKEN, CHAT)
    assert path.read_text() == "OTHER=keep\n"
    assert not list(tmp_path.glob('.telegram-env-*'))


def test_env_symlinks_and_invalid_values_rejected(tmp_path):
    target = tmp_path / "real"
    target.write_text("OTHER=keep\n")
    path = tmp_path / ".env"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        setup.save_credentials(path, TOKEN, CHAT)
    with pytest.raises(ValueError):
        setup.save_credentials(target, TOKEN + "\nBAD=1", CHAT)
    assert target.read_text() == "OTHER=keep\n"


def update(chat, text, kind="private"):
    return {"message": {"chat": {"id": chat, "type": kind}, "text": text}}


def test_chat_discovery_never_uses_first_chat_or_group():
    challenge = "/start unique-confirmation"
    updates = [update(123, "old-message"), update(456, challenge, "group"), update(789, challenge)]
    assert setup.matching_chat(updates, challenge) == "789"
    with pytest.raises(ValueError):
        setup.matching_chat(updates[:2], challenge)
    with pytest.raises(ValueError):
        setup.matching_chat(updates + [update(987, challenge)], challenge)


@pytest.mark.parametrize("confirm", ["y", "n"])
def test_setup_confirms_destination_and_tests_before_save(tmp_path, monkeypatch, capsys, confirm):
    path = tmp_path / ".env"
    monkeypatch.setattr(setup, "ENV_PATH", path)
    monkeypatch.setattr(setup, "getpass", lambda _: TOKEN)
    answers = iter([CHAT, confirm])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    api = Mock(side_effect=[{"username": "TestBot"}, {"first_name": "Owner"}, {"message_id": 1}])
    monkeypatch.setattr(setup, "api_call", api)
    assert setup.main() == 0
    assert path.exists() == (confirm == "y")
    assert api.call_count == (3 if confirm == "y" else 2)
    assert TOKEN not in capsys.readouterr().out


def test_telegram_http_requires_login_csrf_and_hides_secrets(server, tmp_path):
    notifier = tg.TelegramNotifier(tmp_path, "dry-run", lambda: {}, environ=ENV, sender=Mock())
    server.telegram = notifier
    assert request(server, "GET", "/api/telegram")[0] == 401
    assert request(server, "POST", "/api/telegram/test", {})[0] == 401
    cookie = login(server)
    assert request(server, "POST", "/api/telegram/test", {}, cookie=cookie,
                   extra={"Origin": "https://other.example"})[0] == 403
    assert not notifier.path.exists()
    assert request(server, "POST", "/api/telegram/test", {}, cookie=cookie)[0] == 202
    assert request(server, "POST", "/api/telegram/test", {}, cookie=cookie)[0] == 409
    status, _, body = request(server, "GET", "/api/telegram", cookie=cookie)
    assert status == 200 and json.loads(body)["pending"] == 1
    assert TOKEN.encode() not in body and CHAT.encode() not in body
    notifier.sender.assert_not_called()
    assert request(server, "GET", "/assets/telegram.js")[0] == 200
    assert request(server, "POST", "/api/settings", {"telegram_orders": False}, cookie=cookie)[0] == 200
    assert main.load_trading_settings()["telegram_orders"] is False
    assert request(server, "POST", "/api/settings", {"telegram_orders": "bad"}, cookie=cookie)[0] == 400


def test_analysis_notification_explains_not_a_fill(service):
    notifier, _, _, _ = service
    tg.analysis_notification(SimpleNamespace(_telegram=notifier), {"summaryPath": "unique", "analyses": [
        {"ticker": "005930", "name": "삼성전자", "intradayContract": {"action": "HOLD", "reasons": ["거래량 부족"]}}]})
    message = rows(notifier)[0]["text"]
    assert "삼성전자" in message and "HOLD" in message and "체결 내역이 아닙니다" in message


def test_partial_analysis_uses_error_category_and_bad_format_is_nonfatal(service):
    notifier, _, _, settings = service
    settings['telegram_analysis'] = False
    args = SimpleNamespace(_telegram=notifier)
    tg.analysis_notification(args, {'ok': False, 'selectionReason': '시장 자료 확인 실패'})
    assert rows(notifier)[0]['category'] == 'errors'
    tg.analysis_notification(args, {'analyses': None})
    assert notifier.error == '분석 알림 생성 실패'


def test_background_worker_drains_shutdown_queue(tmp_path):
    sent = []
    notifier = tg.TelegramNotifier(tmp_path, 'dry-run', lambda: {}, environ=ENV, sender=sent.append)
    notifier.emit('system', 'start')
    notifier.emit('system', 'stop')
    notifier.start()
    notifier.stop()
    assert len(sent) == 2
    assert not notifier.thread.is_alive()


def test_worker_start_failure_does_not_stop_trading(service, monkeypatch):
    notifier, _, _, _ = service
    monkeypatch.setattr(tg.threading.Thread, 'start', Mock(side_effect=RuntimeError('synthetic')))
    notifier.start()
    assert notifier.thread is None and notifier.error == '알림 작업자 시작 실패'
    notifier.stop()


def test_runtime_lifecycle_reuses_settings_and_never_calls_broker(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    args = main.make_parser().parse_args(["--scenario", "auto-paper"])
    args.monitor = False
    manager = Mock()
    manager.store.path = tmp_path / "positions.sqlite3"
    monkeypatch.setattr(main, "PositionRiskManager", Mock(return_value=manager))
    emitted = []
    monkeypatch.setattr(tg.TelegramNotifier, "start", lambda self: None)
    monkeypatch.setattr(tg.TelegramNotifier, "stop", lambda self: None)
    monkeypatch.setattr(tg.TelegramNotifier, "emit", lambda self, category, title, *a, **k: emitted.append((category, title)))
    async def run(actual):
        assert isinstance(actual._telegram, tg.TelegramNotifier)
        assert actual._telegram.mode == "dry-run"
        assert actual._telegram.settings()["telegram_orders"] is True
        return 0
    monkeypatch.setattr(main, "run_scenarios", run)
    assert asyncio.run(main._main_async(args)) == 0
    assert emitted == [("system", "프로그램 시작"), ("system", "프로그램 종료")]
    manager.start.assert_called_once()
    manager.stop.assert_called_once()
