"""Durable, mode-isolated experience retrieval; never model-weight training.

Only previous trading dates are retrieved. Order submission/recording is not
evidence of a fill or profitability, and is never converted to a reward.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4


ROLE_FIELDS = {
    "market_analyst": ("market_report",),
    "sentiment_analyst": ("sentiment_report",),
    "news_analyst": ("news_report",),
    "fundamentals_analyst": ("fundamentals_report",),
    "bull_researcher": ("investment_debate_state", "bull_history"),
    "bear_researcher": ("investment_debate_state", "bear_history"),
    "research_manager": ("investment_plan",),
    "trader": ("trader_investment_plan",),
    "risk_aggressive": ("risk_debate_state", "aggressive_history"),
    "risk_neutral": ("risk_debate_state", "neutral_history"),
    "risk_conservative": ("risk_debate_state", "conservative_history"),
    "portfolio_manager": ("final_trade_decision",),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class ExperienceStore:
    """SQLite transactions keep analysis and broker events linked by run ID."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, ticker TEXT NOT NULL,
                    trade_date TEXT NOT NULL, started_at TEXT NOT NULL,
                    objective TEXT NOT NULL, routing TEXT NOT NULL,
                    reports TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'STARTED'
                );
                CREATE INDEX IF NOT EXISTS runs_retrieval
                    ON runs(mode, trade_date, ticker);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at TEXT NOT NULL, role TEXT NOT NULL, lesson TEXT NOT NULL,
                    source TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, *, mode, ticker, trade_date, objective, routing):
        if mode not in {"dry-run", "live"}:
            raise ValueError("Experience mode must be dry-run or live.")
        date.fromisoformat(trade_date)
        run_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,mode,ticker,trade_date,started_at,objective,routing) VALUES(?,?,?,?,?,?,?)",
                (run_id, mode, ticker, trade_date, utc_now(), encode(objective), encode(routing)),
            )
        return run_id

    def save_analysis(self, run_id, state):
        reports = {}
        for role, fields in ROLE_FIELDS.items():
            value = state
            for field in fields:
                value = value.get(field, "") if isinstance(value, dict) else ""
            if isinstance(value, str) and value.strip():
                reports[role] = value
        payload = {"reports": reports, "validation": state.get("local_safe_validation", {}),
                   "finalRating": state.get("final_rating"), "newsExecution": state.get("news_execution")}
        with self.connect() as db:
            previous = db.execute("SELECT payload FROM events WHERE run_id=? AND kind='ANALYSIS' ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
            if previous and json.loads(previous["payload"]) == payload:
                return
            if db.execute("UPDATE runs SET reports=?,status='ANALYZED' WHERE id=?", (encode(reports), run_id)).rowcount != 1:
                raise ValueError("Unknown experience run ID.")
            db.execute("INSERT INTO events(run_id,created_at,kind,payload) VALUES(?,?,?,?)",
                       (run_id, utc_now(), "ANALYSIS", encode(payload)))

    def event(self, run_id, kind, payload):
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,created_at,kind,payload) VALUES(?,?,?,?)",
                       (run_id, utc_now(), kind, encode(payload)))
            db.execute("UPDATE runs SET status=? WHERE id=?", (kind, run_id))

    def add_feedback(self, run_id, role, lesson, source):
        """Explicit human review, not an automatically verified profit label."""
        if role not in ROLE_FIELDS or not lesson.strip() or not source.strip():
            raise ValueError("A valid agent role, lesson and evidence source are required.")
        if len(lesson) > 2000 or len(source) > 1000:
            raise ValueError("Feedback is too long (lesson: 2000, source: 1000 characters).")
        with self.connect() as db:
            db.execute("INSERT INTO feedback(run_id,created_at,role,lesson,source) VALUES(?,?,?,?,?)",
                       (run_id, utc_now(), role, lesson, source))

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown experience run ID.")
            result = dict(row)
            for key in ("objective", "routing", "reports"):
                result[key] = json.loads(result[key])
            result["events"] = [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))]
            for event in result["events"]:
                event["payload"] = json.loads(event["payload"])
            result["feedback"] = [dict(r) for r in db.execute("SELECT * FROM feedback WHERE run_id=? ORDER BY id", (run_id,))]
            return result

    def recent(self, mode, limit=20):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,mode,ticker,trade_date,started_at,status FROM runs WHERE mode=? ORDER BY started_at DESC LIMIT ?",
                (mode, min(max(int(limit), 1), 100)),
            )]

    def contexts(self, run_id):
        current = self.get(run_id)
        contexts = {}
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM runs WHERE mode=? AND trade_date<? AND started_at<?
                   AND reports!='{}' ORDER BY (ticker=?) DESC,trade_date DESC,started_at DESC LIMIT 100""",
                (current["mode"], current["trade_date"], current["started_at"], current["ticker"]),
            ).fetchall()
            for role in ROLE_FIELDS:
                examples = []
                for row in rows:
                    if json.loads(row["objective"]) != current["objective"]:
                        continue
                    events = db.execute(
                        "SELECT kind,payload FROM events WHERE run_id=? AND created_at<? ORDER BY id",
                        (row["id"], current["started_at"]),
                    ).fetchall()
                    analyses = [json.loads(e["payload"]) for e in events if e["kind"] == "ANALYSIS"]
                    report = analyses[-1].get("reports", {}).get(role) if analyses else None
                    if not report:
                        continue
                    if analyses[-1].get("finalRating") == "REVIEW":
                        continue
                    # Never feed raw broker responses/account data into prompts.
                    processing = [e["kind"] for e in events]
                    validation = [json.loads(e["payload"]).get("validation", {}) for e in events if e["kind"] == "ANALYSIS"]
                    plans = [json.loads(e["payload"]).get("plan", {}) for e in events if e["kind"] == "PLANNED"]
                    executions = [json.loads(e["payload"]).get("execution", {}) for e in events if e["kind"] == "RESULT"]
                    execution = executions[-1] if executions else {}
                    feedback = [dict(r) for r in db.execute(
                        "SELECT lesson,source FROM feedback WHERE run_id=? AND role=? AND created_at<? ORDER BY id DESC LIMIT 1",
                        (row["id"], role, current["started_at"]),
                    )]
                    examples.append({
                        "run_id": row["id"], "date": row["trade_date"], "ticker": row["ticker"],
                        "agent": role, "model": json.loads(row["routing"]).get(role),
                        "past_analysis_excerpt": report[:1400], "processing_events": processing[-5:],
                        "plan": plans[-1] if plans else {},
                        "execution": {k: execution[k] for k in ("executed", "recorded", "dryRun", "skipped") if k in execution},
                        "validation": validation[-1:], "human_review": feedback,
                        "performance": "UNKNOWN: no verified fills or realized P&L",
                    })
                    if len(examples) == 3:
                        break
                # Keep each complete JSON example intact within the prompt budget.
                while examples and len(encode(examples)) > 6500:
                    examples.pop()
                if examples:
                    contexts[role] = encode(examples)
        return contexts
