"""A separate, bounded subprocess lane for manual research, never scheduled trades."""

from copy import deepcopy
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import threading

from .long_term import now, validate_request, write_json
from .universe import RESEARCH_TIMEOUT_SECONDS, import_csv


class ResearchBusy(ValueError):
    pass


def read(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


class ResearchService:
    def __init__(self, root, project):
        self.root, self.project = Path(root), Path(project)
        self.lock = threading.RLock()
        self.process = None
        self.job_id = None

    def _file_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        stream = (self.root / "worker.lock").open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise ResearchBusy("다른 중장기 탐색이 실행 중입니다.") from None
        return stream

    def snapshot(self, job_id=None):
        with self.lock:
            latest = read(self.root / "latest.json", {})
            selected = job_id or latest.get("id")
            if selected and not re.fullmatch(r"[a-f0-9]{32}", selected):
                raise ValueError("잘못된 탐색 ID입니다.")
            status = read(self.root / selected / "status.json", {}) if selected else {}
            report = read(self.root / selected / "report.json") if selected else None
            active = read(self.root / latest["id"] / "status.json", {}) if latest.get("id") else {}
            if active.get("status") == "running":
                try:
                    stream = self._file_lock()
                except ResearchBusy:
                    pass
                else:
                    stream.close()
                    active = {**active, "status": "interrupted", "phase": "프로세스가 종료되었습니다. 자동 재실행하지 않습니다."}
                    if selected == latest.get("id"):
                        status = active
            history = []
            for path in sorted(self.root.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                item = read(path, {})
                if item.get("id") == active.get("id"):
                    item = active
                history.append(item)
            return deepcopy({"available": True, "active": active if active.get("status") == "running" else None,
                "status": status, "report": report, "history": history,
                "universe": read(self.root / "universe.json"),
                "canStop": bool(self.process and self.process.poll() is None)})

    def submit(self, payload, settings):
        config = validate_request(payload, settings)
        job_id = config["id"]
        with self.lock:
            directory = self.root / job_id
            existing = read(directory / "request.json")
            if existing is not None:
                if existing != config:
                    raise ValueError("이미 다른 요청에 사용한 ID입니다.")
                return self.snapshot(job_id)
            stream = self._file_lock()
            try:
                write_json(directory / "request.json", config)
                write_json(directory / "status.json", {"id": job_id, "status": "running", "phase": "시작 대기", "updatedAt": now()})
                write_json(self.root / "latest.json", {"id": job_id})
                # The inherited descriptor holds the lock even if the dashboard restarts.
                process = subprocess.Popen([sys.executable, "-m", "tradingagents.research.long_term", str(directory)],
                    cwd=self.project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    pass_fds=(stream.fileno(),), start_new_session=True)
                self.process, self.job_id = process, job_id
            except OSError:
                write_json(directory / "status.json", {"id": job_id, "status": "failed", "phase": "탐색 프로세스 시작 실패", "updatedAt": now()})
                raise ValueError("탐색 프로세스를 시작하지 못했습니다.") from None
            finally:
                stream.close()
            threading.Thread(target=self._watch, args=(process, job_id), daemon=True).start()
            return self.snapshot(job_id)

    def _watch(self, process, job_id):
        timed_out = False
        try:
            process.wait(timeout=RESEARCH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        with self.lock:
            path = self.root / job_id / "status.json"
            status = read(path, {})
            if status.get("status") == "running":
                write_json(path, {"id": job_id, "status": "failed", "updatedAt": now(),
                    "phase": f"탐색 제한시간({RESEARCH_TIMEOUT_SECONDS // 60}분) 초과" if timed_out else "탐색 프로세스 종료; 새 실행이 필요합니다."})

    def register_universe(self, payload):
        snapshot = import_csv(payload)
        with self.lock:
            stream = self._file_lock()
            try:
                write_json(self.root / "universe.json", snapshot)
            finally:
                stream.close()
            return self.snapshot()

    def stop(self, job_id):
        with self.lock:
            if not self.process or self.process.poll() is not None or job_id != self.job_id:
                raise ResearchBusy("이 대시보드에서 실행 중인 탐색이 아닙니다.")
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            write_json(self.root / job_id / "status.json", {"id": job_id, "status": "stopped", "phase": "사용자가 탐색을 중단했습니다.", "updatedAt": now()})
            return self.snapshot(job_id)

    def close(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.stop(self.job_id)
