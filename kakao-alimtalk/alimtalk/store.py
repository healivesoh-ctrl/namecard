"""SQLite 저장소: 응답(submissions)·발송 작업(jobs)·수신거부(optouts).

서버 1대 전제. 파일 경로는 DB_PATH 환경변수(기본 data/alimtalk.db).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .models import Job, Submission

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  form_id TEXT NOT NULL,
  phone TEXT NOT NULL,
  name TEXT DEFAULT '',
  answers TEXT NOT NULL,
  source TEXT DEFAULT 'webhook',
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sub_phone ON submissions(phone, received_at);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER,
  form_id TEXT NOT NULL,
  message_name TEXT NOT NULL,
  phone TEXT NOT NULL,
  template_code TEXT NOT NULL,
  variables TEXT NOT NULL,
  text TEXT,
  subject TEXT,
  scheduled_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  sent_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs(status, scheduled_at);
CREATE TABLE IF NOT EXISTS optouts (
  phone TEXT PRIMARY KEY,
  reason TEXT,
  created_at TEXT NOT NULL
);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str | Path | None = None):
        p = Path(path or os.environ.get("DB_PATH") or BASE_DIR / "data" / "alimtalk.db")
        if str(p) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ── 응답 ───────────────────────────────────────────────
    def add_submission(self, sub: Submission) -> Submission:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO submissions(form_id, phone, name, answers, source, received_at) VALUES (?,?,?,?,?,?)",
                (sub.form_id, sub.phone, sub.name, json.dumps(sub.answers, ensure_ascii=False),
                 sub.source, _iso(sub.received_at or datetime.now(timezone.utc))),
            )
            self._conn.commit()
            sub.id = cur.lastrowid
            return sub

    def recent_submission(self, form_id: str, phone: str, within_hours: float) -> bool:
        if within_hours <= 0:
            return False
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=within_hours))
        row = self._conn.execute(
            "SELECT 1 FROM submissions WHERE form_id=? AND phone=? AND received_at>=? LIMIT 1",
            (form_id, phone, since)).fetchone()
        return row is not None

    def list_submissions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM submissions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["answers"] = json.loads(d["answers"])
            out.append(d)
        return out

    # ── 발송 작업 ───────────────────────────────────────────
    def add_job(self, job: Job) -> Job:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs(submission_id, form_id, message_name, phone, template_code, variables, text, subject,"
                " scheduled_at, status, attempts, result, sent_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.submission_id, job.form_id, job.message_name, job.phone, job.template_code,
                 json.dumps(job.variables, ensure_ascii=False), job.text, job.subject,
                 _iso(job.scheduled_at), job.status, job.attempts,
                 json.dumps(job.result, ensure_ascii=False) if job.result else None,
                 _iso(job.sent_at), _iso(datetime.now(timezone.utc))),
            )
            self._conn.commit()
            job.id = cur.lastrowid
            return job

    def _row_to_job(self, r: sqlite3.Row) -> Job:
        return Job(
            id=r["id"], submission_id=r["submission_id"], form_id=r["form_id"],
            message_name=r["message_name"], phone=r["phone"], template_code=r["template_code"],
            variables=json.loads(r["variables"] or "{}"), text=r["text"], subject=r["subject"],
            scheduled_at=_parse(r["scheduled_at"]), status=r["status"], attempts=r["attempts"],
            result=json.loads(r["result"]) if r["result"] else None,
            sent_at=_parse(r["sent_at"]), created_at=_parse(r["created_at"]),
        )

    def get_job(self, job_id: int) -> Job | None:
        r = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(r) if r else None

    def due_jobs(self, now: datetime | None = None, limit: int = 50) -> list[Job]:
        now_s = _iso(now or datetime.now(timezone.utc))
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at LIMIT ?",
            (now_s, limit)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def list_jobs(self, status: str | None = None, limit: int = 200) -> list[Job]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update_job(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, attempts=?, result=?, sent_at=?, scheduled_at=? WHERE id=?",
                (job.status, job.attempts, json.dumps(job.result, ensure_ascii=False) if job.result else None,
                 _iso(job.sent_at), _iso(job.scheduled_at), job.id))
            self._conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        out = {r["status"]: r["c"] for r in rows}
        out["submissions"] = self._conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        out["optouts"] = self._conn.execute("SELECT COUNT(*) FROM optouts").fetchone()[0]
        return out

    # ── 수신거부 ───────────────────────────────────────────
    def add_optout(self, phone: str, reason: str = "") -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO optouts(phone, reason, created_at) VALUES (?,?,?)",
                               (phone, reason, _iso(datetime.now(timezone.utc))))
            # 아직 안 나간 예약도 취소
            self._conn.execute("UPDATE jobs SET status='cancelled' WHERE phone=? AND status='pending'", (phone,))
            self._conn.commit()

    def remove_optout(self, phone: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM optouts WHERE phone=?", (phone,))
            self._conn.commit()

    def is_opted_out(self, phone: str) -> bool:
        return self._conn.execute("SELECT 1 FROM optouts WHERE phone=?", (phone,)).fetchone() is not None

    def list_optouts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM optouts ORDER BY created_at DESC")]

    def close(self) -> None:
        self._conn.close()
