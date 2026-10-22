"""Bounded, same-session retry of BUY plans, never reinterpret HOLD as BUY."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
import threading
import uuid

from .intraday import INTRADAY_DEFAULTS
from .toss_executor import TossOrderPlan


class EntryWatch:
    def __init__(self, manager, guard=None):
        self.manager = manager
        self.guard = guard or (lambda: "ready")
        self.session = uuid.uuid4().hex
        self.worker_lock = threading.Lock()

    def settings(self):
        return {**INTRADAY_DEFAULTS, **self.manager.settings()}

    @staticmethod
    def signature(settings):
        return json.dumps(settings, sort_keys=True, ensure_ascii=False)

    def bar_key(self):
        now = self.manager.clock()
        return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0).isoformat()

    def cancel(self, symbol=None, reason="cancelled", expected_id=None):
        manager = self.manager
        with manager.store.locked():
            state = manager.store.load()
            if expected_id is not None:
                row = state.get("entryWatches", {}).get(symbol)
                if not row or row["id"] != expected_id or row["status"] not in {"waiting", "checking"}:
                    raise ValueError("감시 후보가 변경되었거나 이미 처리되었습니다.")
            for key, row in state.get("entryWatches", {}).items():
                if (symbol is None or key == symbol) and row["status"] in {"waiting", "checking"}:
                    row.update(status="cancelled", reason=reason)
            manager.store.save(state)

    def enqueue(self, plan, experience_id, expected_settings):
        manager, settings = self.manager, self.settings()
        minutes = int(settings["entry_watch_minutes"])
        if plan.action != "BUY" or plan.rating not in {"Buy", "Overweight"} or minutes <= 0:
            return None
        if self.signature(settings) != expected_settings or self.guard() == "cancel" or manager.stop_event.is_set():
            return None
        now = manager.clock()
        window = manager._window(now, settings)
        cutoff = window[1] - timedelta(minutes=int(settings.get("buy_cutoff_minutes", 30))) if window else now
        if not window or not window[0] <= now < cutoff:
            return None
        with manager.store.locked():
            state = manager.store.load()
            if self.signature(self.settings()) != expected_settings or self.guard() == "cancel" or manager.stop_event.is_set():
                return None
            rows = state.setdefault("entryWatches", {})
            if sum(row["status"] in {"waiting", "checking"} for row in rows.values()) >= 5:
                return None
            rows[plan.symbol] = {"id": uuid.uuid4().hex, "session": self.session, "symbol": plan.symbol,
                "status": "waiting", "reason": "분봉 진입 조건 대기", "plan": plan.__dict__,
                "experienceId": experience_id, "settingsSignature": expected_settings,
                "createdAt": now.isoformat(), "expiresAt": min(now + timedelta(minutes=minutes), cutoff).isoformat(),
                "lastBar": self.bar_key(), "lastCheckedAt": now.isoformat(), "attempts": 0}
            # Retain only a small display history; active items are never evicted.
            for key in list(rows):
                if len(rows) <= 30:
                    break
                if rows[key]["status"] not in {"waiting", "checking"}:
                    del rows[key]
            manager._event(state, "entry-watch-created", symbol=plan.symbol, watchId=rows[plan.symbol]["id"])
            manager.store.save(state)
            return deepcopy(rows[plan.symbol])

    def invalid_reason(self, row):
        manager = self.manager
        now = manager.clock()
        if row["session"] != self.session:
            return "프로그램 재시작: 재분석 필요"
        if manager.stop_event.is_set() or self.guard() == "cancel":
            return "실행 중단 또는 자동 매매 일시정지"
        if datetime.fromisoformat(row["createdAt"]).date() != now.date() or now >= datetime.fromisoformat(row["expiresAt"]):
            return "감시 유효기간 또는 매수 가능 시간 만료"
        if row["settingsSignature"] != self.signature(self.settings()):
            return "매매 설정 변경: 재분석 필요"
        return None

    def valid(self, state, symbol, watch_id):
        row = state.get("entryWatches", {}).get(symbol)
        if not row or row["id"] != watch_id or row["status"] != "checking":
            return False
        reason = self.invalid_reason(row)
        if reason or self.guard() != "ready":
            row.update(status="cancelled", reason=reason or "새 분석/사용자 작업 시작: 재분석 필요")
            return False
        return True

    def process(self):
        # This worker is separate from the position-exit loop; slow charts cannot stop exit checks.
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            self._process()
        finally:
            self.worker_lock.release()

    def _process(self):
        manager = self.manager
        with manager.store.locked():
            state = manager.store.load()
            candidates = []
            for row in state.get("entryWatches", {}).values():
                if row["status"] not in {"waiting", "checking"}:
                    continue
                reason = self.invalid_reason(row)
                if reason:
                    row.update(status="cancelled", reason=reason)
                elif row["status"] == "waiting" and row["lastBar"] != self.bar_key():
                    candidates.append(row)
            if not candidates or self.guard() != "ready":
                manager.store.save(state)
                return
            row = min(candidates, key=lambda value: value["lastCheckedAt"])
            row.update(status="checking", lastBar=self.bar_key(), lastCheckedAt=manager.clock().isoformat(),
                       attempts=row["attempts"] + 1)
            chosen = deepcopy(row)
            manager.store.save(state)
        try:
            result = manager.execute(TossOrderPlan(**chosen["plan"]), chosen["experienceId"], entry_watch_id=chosen["id"])
            if result.get("skipped") == "intraday-filter":
                update = {"status": "waiting", "reason": "; ".join(result.get("entryCheck", {}).get("reasons", []))}
            elif result.get("skipped"):
                update = {"status": "cancelled", "reason": result["skipped"]}
            else:
                update = {"status": "submitted", "reason": "주문 결과: " + str(result.get("result", {}).get("status", "미확인"))}
        except Exception as exc:
            # An uncertain submission is terminal here. The order ledger reconciles it; never retry it.
            update = {"status": "cancelled", "reason": f"재점검/주문 중단 ({type(exc).__name__}): {exc}"}
        with manager.store.locked():
            state = manager.store.load()
            row = state.get("entryWatches", {}).get(chosen["symbol"])
            if row and row["id"] == chosen["id"] and row["status"] == "checking":
                row.update(update)
                manager._event(state, "entry-watch-result", symbol=chosen["symbol"], watchId=chosen["id"], status=row["status"])
                manager.store.save(state)
