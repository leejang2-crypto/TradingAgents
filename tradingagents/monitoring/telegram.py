"""Outbound-only Telegram alerts. No broker mutations, LLM calls or inbound commands."""

from datetime import datetime
from decimal import Decimal
from contextlib import closing, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import certifi

PREFIX = "TRADINGAGENTS_TELEGRAM_"
CATEGORIES = {"system": "시작·종료", "analysis": "분석 결과", "orders": "주문·체결",
              "risk": "매매 제한·청산", "errors": "오류·LLM 전환", "review": "장 마감 복기"}
DEFAULTS = {"telegram_enabled": True, **{"telegram_" + key: True for key in CATEGORIES}}
KST = ZoneInfo("Asia/Seoul")


def credentials(environ=None):
    values = os.environ if environ is None else environ
    token = values.get(PREFIX + "BOT_TOKEN", "").strip()
    chat = values.get(PREFIX + "CHAT_ID", "").strip()
    enabled = values.get(PREFIX + "ENABLED", "").lower() in {"1", "true", "yes", "on"}
    valid = bool(re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token)
                 and re.fullmatch(r"-?[1-9][0-9]{0,19}", chat))
    return token, chat, enabled, valid


def clean(value):
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", str(value))
    text = re.sub(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}|sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:500]


class TelegramError(Exception):
    def __init__(self, code, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram {code}")


def api_call(token, method, payload, *, connection_factory=http.client.HTTPSConnection):
    # Use a non-logging transport: HTTP client INFO logs can otherwise expose the token URL.
    if method not in {"getMe", "getUpdates", "getChat", "sendMessage"}:
        raise ValueError("Unsupported Telegram method")
    connection = None
    try:
        # Framework Python on macOS may lack its default CA file; keep full TLS verification.
        context = ssl.create_default_context(cafile=certifi.where())
        connection = connection_factory("api.telegram.org", timeout=5, context=context)
        connection.request("POST", f"/bot{token}/{method}", json.dumps(payload).encode("utf-8"),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        status = response.status
        data = json.loads(response.read(1_000_001))
    except ssl.SSLCertVerificationError:
        raise TelegramError("tls-certificate") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise TelegramError("network-or-response") from None
    finally:
        if connection:
            connection.close()
    if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
        code = data.get("error_code", status) if isinstance(data, dict) else status
        code = code if type(code) is int else status
        parameters = data.get("parameters") if isinstance(data, dict) else None
        retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
        raise TelegramError(code, retry)
    result = data.get("result")
    valid = isinstance(result, list) if method == "getUpdates" else isinstance(result, dict)
    if valid and method == "sendMessage":
        valid = type(result.get("message_id")) is int
    if not valid:
        raise TelegramError("network-or-response")
    return result


class TelegramNotifier:
    def __init__(self, local_dir, mode, settings, *, risk_path=None, environ=None, sender=None, clock=time.time):
        self.path = Path(local_dir) / "notifications" / "telegram.sqlite3"
        self.mode, self.settings, self.risk_path = mode, settings, risk_path
        self.token, self.chat, self.env_enabled, self.configured = credentials(environ)
        self.destination = hashlib.sha256((self.token + ":" + self.chat).encode()).hexdigest()
        self.sender = sender or (lambda text: api_call(self.token, "sendMessage", {
            "chat_id": self.chat, "text": text, "link_preview_options": {"is_disabled": True}}))
        self.clock, self.started_at = clock, clock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread, self.error, self.pause_until = None, None, 0
        self._last_test = float('-inf')
        self._test_lock = threading.Lock()
        self._cursors = {}
        self.shutdown_at = 0

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=.1)
        try:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, destination TEXT, mode TEXT, category TEXT, text TEXT, created REAL, expires REAL, due REAL, attempts INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT, sent REAL)")
                db.execute("CREATE TABLE IF NOT EXISTS cursors (id TEXT PRIMARY KEY, signature TEXT)")
                self.path.chmod(0o600)
                yield db
        finally:
            db.close()

    def enabled(self, category=None):
        cfg = {**DEFAULTS, **self.settings()}
        return (self.env_enabled and self.configured and cfg["telegram_enabled"]
                and (category is None or category == "test" or cfg.get("telegram_" + category, False)))

    def emit(self, category, title, lines=(), *, key=None):
        try:
            if not self.enabled(category):
                return False
            stamp = datetime.fromtimestamp(self.clock(), KST).isoformat(timespec="seconds")
            label = "실투자" if self.mode == "live" else "모의투자"
            message = "\n".join([f"[TradingAgents · {label}] {clean(title)}", stamp,
                                  *[clean(line) for line in list(lines)[:12]]])[:3800]
            ident = hashlib.sha256(f"{self.destination}:{self.mode}:{key or uuid.uuid4().hex}".encode()).hexdigest()
            with self._db() as db:
                if db.execute("SELECT count(*) FROM alerts WHERE status='pending'").fetchone()[0] >= 500:
                    self.error = "알림 대기열 한도 초과"
                    return False
                db.execute("INSERT OR IGNORE INTO alerts(id,destination,mode,category,text,created,expires,due) VALUES(?,?,?,?,?,?,?,?)",
                           (ident, self.destination, self.mode, category, message, self.clock(), self.clock() + 3600, self.clock()))
            self.wake.set()
            return True
        except Exception:
            self.error = "알림 설정/저장 실패"
            return False

    def test(self):
        with self._test_lock:
            if self.clock() - self._last_test < 30:
                return False
            queued = self.emit("test", "연결 테스트", ["알림 전용입니다. 매매 주문은 실행하지 않았습니다."])
            if queued:
                self._last_test = self.clock()
            return queued

    def status(self):
        result = {"available": True, "configured": self.configured, "environmentEnabled": self.env_enabled,
                  "enabled": False, "mode": self.mode, "error": self.error, "recent": []}
        try:
            result["enabled"] = bool(self.enabled())
            if self.path.exists():
                with self._db() as db:
                    result["recent"] = [dict(row) for row in db.execute(
                        "SELECT category,status,created,sent,attempts,error FROM alerts WHERE destination=? AND mode=? ORDER BY created DESC LIMIT 8",
                        (self.destination, self.mode))]
                    result["pending"] = db.execute("SELECT count(*) FROM alerts WHERE destination=? AND mode=? AND status='pending'",
                                                   (self.destination, self.mode)).fetchone()[0]
        except Exception:
            result["error"] = "알림 상태 조회 실패"
        return result

    def _changed(self, key, signature, category, title, lines, *, baseline=False):
        ident = f"{self.destination}:{self.mode}:{key}"
        encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True)
        if self._cursors.get(ident) == encoded:
            return
        with self._db() as db:
            previous = db.execute("SELECT signature FROM cursors WHERE id=?", (ident,)).fetchone()
        if previous and previous[0] == encoded:
            self._cursors[ident] = encoded
            return
        if not baseline or previous:
            if not self.emit(category, title, lines, key=ident + ":" + encoded):
                return
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (ident, encoded))
        self._cursors[ident] = encoded

    def observe_positions(self):
        if not self.enabled() or not self.risk_path or not Path(self.risk_path).exists():
            return
        # Read the committed ledger without taking the broker's execution lock.
        with closing(sqlite3.connect(Path(self.risk_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            row = db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        if not row:
            return
        state = json.loads(row[0])
        for key, order in state.get("orders", {}).items():
            payload = order["payload"]
            status, quantity = order["status"], order.get("appliedQty", "0")
            created = datetime.fromisoformat(order["createdAt"]).timestamp()
            old = created < self.started_at
            lines = [f"{payload['symbol']} · {payload['side']} · 상태 {status}"]
            if Decimal(quantity) > 0:
                detail = (order.get("detail") or {}).get("execution") or {}
                amount = detail.get("filledAmount")
                average = str((Decimal(amount) / Decimal(quantity)).quantize(Decimal('.01'))) if amount else "확인 중"
                lines.append(f"확인된 누적 체결 {quantity}주 · 평균 체결가 {average}원")
                if order.get("financialsIncomplete"):
                    lines.append("체결 금액 또는 비용 확인 중")
                title = "전량 체결 확인" if status in {"FILLED", "SIMULATED_FILLED"} else "부분 체결/주문 상태 변경"
            else:
                lines.append(f"주문 수량 {payload.get('quantity', '확인 중')}주 · 체결 완료 아님")
                title = "주문 접수" if status == "PENDING" else "주문 상태 변경"
            if status == "SUBMITTING":
                continue
            category = "errors" if status in {"UNKNOWN", "REJECTED", "SIMULATED_REJECTED"} else "orders"
            self._changed("order:" + key, [status, quantity, order.get("financialsIncomplete")],
                          category, title, lines, baseline=old)
        for symbol, position in state.get("positions", {}).items():
            if Decimal(position["quantity"]) <= 0:
                continue
            signature = {key: position.get(key) for key in ("cycle", "liquidate", "exitReason", "carryApproved", "deadlineExceeded", "alert")}
            if any(signature[key] for key in ("liquidate", "carryApproved", "deadlineExceeded", "alert")):
                self._changed("position:" + symbol, signature, "risk", "청산/보유 관리", [
                    f"{symbol} · 보유 {position['quantity']}주",
                    f"청산 기한 {position.get('liquidationDeadlineAt', '확인 중')}",
                    f"사유 {position.get('exitReason') or ('이월 승인' if position.get('carryApproved') else '상태 확인')}",
                    "기한 초과: 체결 확인 필요" if position.get("deadlineExceeded") else "",
                    position.get("alert") or ""])
        daily = state.get("daily") or {}
        for key in ("lossLimitReached", "stopLimitReached"):
            if daily.get(key):
                day = str(state.get("checkedAt", ""))[:10]
                self.emit("risk", "신규 매수 제한", ["일일 손실 한도 또는 연속 손절 한도 도달"], key=f"limit:{day}:{key}")
        if state.get("error"):
            day = str(state.get("checkedAt", ""))[:10]
            self.emit("errors", "보유 관리 점검 실패", ["대시보드에서 오류와 미체결 주문을 확인하세요."], key="risk-error:" + day)

    def deliver_one(self):
        now = self.clock()
        with self._db() as db:
            db.execute("UPDATE alerts SET status='expired' WHERE status='pending' AND expires<=?", (now,))
            db.execute("UPDATE alerts SET status='cancelled' WHERE status='pending' AND destination!=?", (self.destination,))
            db.execute("DELETE FROM alerts WHERE created<? AND status!='pending'", (now - 7 * 86400,))
            row = db.execute("SELECT * FROM alerts WHERE destination=? AND mode=? AND status='pending' AND due<=? ORDER BY created LIMIT 1",
                             (self.destination, self.mode, now)).fetchone()
        if not row or now < self.pause_until:
            return False
        if not self.enabled(row["category"]):
            with self._db() as db:
                db.execute("UPDATE alerts SET status='cancelled' WHERE id=?", (row["id"],))
            return False
        self.pause_until = now + 1.1
        attempts, error, sent, status, due = row["attempts"] + 1, None, None, "sent", now
        try:
            self.sender(row["text"])
            sent = self.clock()
            self.error = None
        except Exception as exc:
            code = exc.code if isinstance(exc, TelegramError) else "network-or-response"
            error = f"Telegram {code}"
            retryable = code == "network-or-response" or code == 429 or (isinstance(code, int) and code >= 500)
            status = "pending" if retryable and attempts < 4 else "failed"
            delay = 10 * 2 ** (attempts - 1)
            if isinstance(exc, TelegramError) and isinstance(exc.retry_after, (int, float)):
                delay = max(delay, exc.retry_after)
            due = now + delay
            if code in {401, 403, 429}:
                self.pause_until = due if code == 429 else now + 300
            self.error = error
        with self._db() as db:
            db.execute("UPDATE alerts SET status=?,attempts=?,error=?,sent=?,due=? WHERE id=?",
                       (status, attempts, error, sent, due, row["id"]))
        return True

    def start(self):
        def loop():
            while not self.stop_event.is_set() or time.monotonic() < self.shutdown_at:
                delivered = False
                try:
                    if self.configured:
                        if not self.stop_event.is_set():
                            self.observe_positions()
                        delivered = self.deliver_one()
                except Exception:
                    self.error = "알림 기록 점검 실패"
                if self.stop_event.is_set() and not delivered and not self.status().get("pending"):
                    break
                self.wake.wait(1.1)
                self.wake.clear()
        self.thread = threading.Thread(target=loop, name="telegram-outbound", daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.thread = None
            self.error = "알림 작업자 시작 실패"

    def stop(self):
        try:
            self.observe_positions()
        except Exception:
            self.error = "종료 시 알림 기록 점검 실패"
        self.shutdown_at = time.monotonic() + 4
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)


def notify(args, category, title, lines=(), *, key=None):
    service = getattr(args, "_telegram", None)
    if isinstance(service, TelegramNotifier):
        service.emit(category, title, lines, key=key)


def analysis_notification(args, summary):
    service = getattr(args, "_telegram", None)
    if not isinstance(service, TelegramNotifier):
        return
    try:
        _analysis_notification(args, summary)
    except Exception:
        service.error = "분석 알림 생성 실패"


def _analysis_notification(args, summary):
    lines = ["분석 판정은 체결 내역이 아닙니다. 주문·체결 알림을 별도로 확인하세요."]
    for row in summary.get("analyses", [])[:8]:
        contract = row.get("intradayContract") or {}
        action = contract.get("action") or (row.get("plan") or {}).get("action") or row.get("decision") or "결과 확인 필요"
        lines.append(f"{row.get('name') or row.get('ticker')} · {clean(action)[:80]}")
        if contract.get("reasons"):
            lines.append("; ".join(contract["reasons"])[:220])
    if len(lines) == 1:
        lines.append(summary.get("selectionReason") or "분석 대상 없음")
    ok = summary.get("ok", True)
    notify(args, "analysis" if ok else "errors", "분석 완료" if ok else "분석 완료 · 일부 오류", lines,
           key="analysis:" + str(summary.get("summaryPath") or uuid.uuid4().hex))
