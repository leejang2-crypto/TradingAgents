"""One execution lane shared by scheduled and user-requested runs."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import uuid

from langchain_core.callbacks import BaseCallbackHandler


CURRENT_CONTROL = ContextVar("trading_execution_control", default=None)
ACTIONS = {"news", "candidates", "analyze", "trade"}


class RunStopped(RuntimeError):
    pass


class BusyError(ValueError):
    pass


class RunController:
    def __init__(self, *, live=False, audit_path: Path | None = None):
        self.live = live
        self.audit_path = audit_path
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.cancel = threading.Event()
        self.active = None
        self.pending = None
        self.last = None
        self.paused = False
        self.requests = {}
        if audit_path and audit_path.exists():
            # Never replay queued/interrupted commands after a process restart.
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row["event"] == "queued":
                    job = row["job"]
                    self.requests[job["id"]] = row["signature"]
                    self.last = {**job, "status": "interrupted", "result": None}
                elif row["event"] == "started":
                    self.last = {**row["job"], "status": "interrupted", "result": None}
                elif row["event"] == "finished":
                    self.last = row["job"]
                elif row["event"] == "pending-cancelled":
                    self.last = {**row["job"], "status": "cancelled", "result": None}
                elif row["event"] == "scheduler-paused":
                    self.paused = row["paused"]

    def _audit(self, event, **details):
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                         "event": event, **details}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.audit_path.chmod(0o600)

    def snapshot(self):
        with self.lock:
            return deepcopy({"available": True, "mode": "live" if self.live else "dry-run",
                             "active": self.active, "pending": self.pending, "last": self.last,
                             "paused": self.paused, "stopRequested": self.cancel.is_set()})

    def submit(self, payload, settings):
        action = payload.get("action")
        request_id = payload.get("requestId")
        policy = payload.get("policy", "ask")
        ticker = payload.get("ticker", "005930.KS")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError("Unknown manual action.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,64}", request_id):
            raise ValueError("Invalid request ID.")
        if not isinstance(policy, str) or policy not in {"ask", "stop", "wait"}:
            raise ValueError("Invalid busy policy.")
        if not isinstance(ticker, str) or not re.fullmatch(r"\d{6}\.(KS|KQ)", ticker):
            raise ValueError("Use a Korean ticker such as 005930.KS.")
        if self.live and action == "trade" and payload.get("confirmLive") is not True:
            raise ValueError("실제 주문 실행 확인이 필요합니다.")
        signature = {key: payload.get(key) for key in ("action", "ticker", "confirmLive")}
        with self.lock:
            if request_id in self.requests:
                if self.requests[request_id] != signature:
                    raise ValueError("Request ID was already used for a different command.")
                return self.snapshot()
            if self.pending:
                raise BusyError("이미 대기 중인 실행 요청이 있습니다.")
            if self.active and (policy == "ask" or payload.get("activeId") != self.active["id"]):
                raise BusyError("현재 작업의 처리 방법을 선택하세요.")
            job = {"id": request_id, "action": action, "ticker": ticker,
                   "settings": deepcopy(settings), "source": "manual",
                   "mode": "live" if self.live else "dry-run"}
            self._audit("queued", job=job, policy=policy, signature=signature)
            self.pending = job
            self.requests[request_id] = signature
            if self.active and policy == "stop":
                self.cancel.set()
            self.wake.set()
            return self.snapshot()

    def pause(self, paused):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean.")
        with self.lock:
            self._audit("scheduler-paused", paused=paused)
            self.paused = paused
            self.wake.set()
            return self.snapshot()

    def cancel_pending(self, request_id):
        with self.lock:
            if not self.pending or request_id != self.pending["id"]:
                raise BusyError("대기 작업이 변경되었거나 이미 시작되었습니다.")
            self._audit("pending-cancelled", job=self.pending)
            self.pending = None
            # A stop already requested for the active run remains in effect.
            return self.snapshot()

    def start(self, scheduled=None):
        with self.lock:
            if self.active:
                raise BusyError("An execution is already active.")
            job = self.pending
            if job is None and not self.paused:
                job = scheduled
            if job is None:
                return None
            job = {**job, "mode": "live" if self.live else "dry-run"}
            self._audit("started", job=job)
            self.pending = None
            self.cancel.clear()
            self.active = {**job, "phase": "starting"}
            return deepcopy(job)

    def finish(self, status, result=None):
        with self.lock:
            record = {**(self.active or {}), "status": status, "result": result}
            self._audit("finished", job=record)
            self.last = record
            self.active = None
            self.cancel.clear()

    def checkpoint(self, phase=None):
        with self.lock:
            if self.cancel.is_set():
                raise RunStopped("사용자 요청으로 중단했습니다. 이미 접수된 주문은 유지됩니다.")
            if self.active and phase:
                self.active["phase"] = phase

    def wait(self, timeout):
        with self.lock:
            if self.pending:
                return
            self.wake.clear()
        self.wake.wait(timeout)

    @contextmanager
    def scope(self):
        token = CURRENT_CONTROL.set(self)
        try:
            yield
        finally:
            CURRENT_CONTROL.reset(token)


def checkpoint(phase=None):
    control = CURRENT_CONTROL.get()
    if control:
        control.checkpoint(phase)


class CancellationCallback(BaseCallbackHandler):
    raise_error = True
    run_inline = True

    def __init__(self, control):
        self.control = control

    def on_chain_start(self, *args, **kwargs):
        self.control.checkpoint("analysis")

    def on_llm_start(self, *args, **kwargs):
        self.control.checkpoint("llm-request")

    def on_chat_model_start(self, *args, **kwargs):
        self.control.checkpoint("llm-request")

    def on_tool_start(self, *args, **kwargs):
        self.control.checkpoint("data-request")


def cancellation_callbacks():
    control = CURRENT_CONTROL.get()
    return [CancellationCallback(control)] if control else None


def scheduled_job():
    return {"id": uuid.uuid4().hex, "action": "trade", "source": "schedule"}
