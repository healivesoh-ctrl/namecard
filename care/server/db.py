"""SQLite 저장소 — 신청서·서류·전화상담·급여 설정.

파일 업로드본과 DB는 DATA_DIR(기본 care/data) 아래 저장된다. 개인정보와
건강 관련 서류가 담기므로 이 디렉터리는 저장소에 커밋하지 않는다(.gitignore).
"""

from __future__ import annotations

import json
import os
import sqlite3
import secrets
import string
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "care.db"

KST = timezone(timedelta(hours=9))

# 필수 서류 4종 — 화면 안내와 검증이 이 정의 하나를 공유한다.
DOC_TYPES: list[dict] = [
    {
        "key": "caregiver_cert",
        "label": "산모신생아건강관리사 교육수료증",
        "desc": "표준교육기관에서 발급한 수료증 사본. 이름·수료일·발급기관이 보이게 촬영/스캔.",
        "required": True,
    },
    {
        "key": "abuse_prevention",
        "label": "아동학대예방교육 수료증",
        "desc": "온라인 이수도 가능하며, 이수증 PDF 또는 캡처본을 올리면 됩니다.",
        "required": True,
    },
    {
        "key": "health_checkup",
        "label": "건강검진결과서",
        "desc": "1년 이내 발급분. 결핵(흉부 X-ray) 항목이 포함되어야 합니다.",
        "required": True,
    },
    {
        "key": "pertussis",
        "label": "백일해 예방접종 증명서류",
        "desc": "Tdap 접종 확인서 또는 예방접종증명서(질병관리청 발급).",
        "required": True,
    },
]
DOC_TYPE_KEYS = {d["key"] for d in DOC_TYPES}
DOC_LABELS = {d["key"]: d["label"] for d in DOC_TYPES}

# 신청 상태 흐름: draft(임시저장) → received(접수, 자동승인) → reviewing(검토)
#                → confirmed(최종확인) / rejected(보완요청)
STATUS_LABELS = {
    "draft": "작성중(임시저장)",
    "received": "접수완료",
    "reviewing": "서류 검토중",
    "rejected": "보완요청(반려)",
    "confirmed": "최종확인 완료",
}
STATUS_ORDER = ["draft", "received", "reviewing", "confirmed"]

SERVICE_DAY_OPTIONS = [5, 10, 15, 20, 25]
DEFAULT_SERVICE_DAYS = 15

DEFAULT_PAY = {
    "effective_month": "",
    "hourly_wage": 0,
    "headline": "친정엄마 산후도우미 예상 소득",
    "note": "최저시급 기준과 매월 이벤트에 따라 변동됩니다. 실제 지급액은 정부지원 유형·이용일수에 따라 달라질 수 있습니다.",
    "rows": [],
    "updated_at": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT UNIQUE NOT NULL,
    token              TEXT UNIQUE NOT NULL,
    mother_name        TEXT NOT NULL DEFAULT '',
    mother_phone       TEXT NOT NULL DEFAULT '',
    helper_name        TEXT NOT NULL DEFAULT '',
    helper_phone       TEXT NOT NULL DEFAULT '',
    helper_relation    TEXT NOT NULL DEFAULT '가족',
    relation_detail    TEXT NOT NULL DEFAULT '',
    due_date           TEXT NOT NULL DEFAULT '',
    service_days       INTEGER NOT NULL DEFAULT 15,
    health_center_code TEXT NOT NULL DEFAULT '',
    memo               TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'draft',
    admin_message      TEXT NOT NULL DEFAULT '',
    missing_docs       TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    submitted_at       TEXT NOT NULL DEFAULT '',
    confirmed_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_app_phone  ON applications(mother_phone);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    doc_type       TEXT NOT NULL,
    filename       TEXT NOT NULL,
    stored_name    TEXT NOT NULL,
    content_type   TEXT NOT NULL DEFAULT '',
    size           INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'uploaded',
    reject_reason  TEXT NOT NULL DEFAULT '',
    uploaded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_app ON documents(application_id);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    message        TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT 'system',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_app ON events(application_id);

CREATE TABLE IF NOT EXISTS consultations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL,
    preferred_time TEXT NOT NULL DEFAULT '',
    memo           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'new',
    admin_note     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""


def now() -> str:
    """KST 기준 ISO 문자열(초 단위)."""
    return datetime.now(KST).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT 1 FROM settings WHERE key='pay'").fetchone() is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('pay', ?)",
                (json.dumps(DEFAULT_PAY, ensure_ascii=False),),
            )


def get_setting(conn: sqlite3.Connection, key: str, default):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def new_code(conn: sqlite3.Connection) -> str:
    """사람이 부르기 쉬운 접수번호. 예: DA-260830-7K3Q"""
    day = datetime.now(KST).strftime("%y%m%d")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 혼동되는 0/O/1/I 제외
    for _ in range(50):
        code = "DA-%s-%s" % (day, "".join(secrets.choice(alphabet) for _ in range(4)))
        if conn.execute("SELECT 1 FROM applications WHERE code=?", (code,)).fetchone() is None:
            return code
    raise RuntimeError("접수번호 생성에 실패했습니다.")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def add_event(conn: sqlite3.Connection, app_id: int, kind: str, message: str, actor: str = "시스템") -> None:
    conn.execute(
        "INSERT INTO events(application_id, kind, message, actor, created_at) VALUES(?,?,?,?,?)",
        (app_id, kind, message, actor, now()),
    )


def normalize_phone(raw: str) -> str:
    """숫자만 남긴 뒤 010-1234-5678 형태로 정규화. 형식이 아니면 원본 유지."""
    digits = "".join(ch for ch in (raw or "") if ch in string.digits)
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 10:
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    return (raw or "").strip()


def phone_digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch in string.digits)
