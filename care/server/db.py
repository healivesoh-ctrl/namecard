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
# 서류마다 어디서 어떻게 발급받는지(issue_url)를 같이 담는다. 발급처를 몰라
# 접수가 막히는 일이 가장 많기 때문이다.
# agency: True 인 서류는 본인 발급이 어려울 때 다이음 다이렉트가 대신 발급해 준다.
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
        "desc": "서울시평생학습포털에서 회원가입 후 온라인으로 수강하실 수 있습니다. "
                "수강을 마치면 수료증을 내려받아 그대로 올려주세요.",
        "required": True,
        "issue_url": (
            "https://sll.seoul.go.kr/lms/requestCourse/doDetailInfo.do"
            "?main_se=lie&course_id=S400620260105102810&class_no=01"
            "&course_gubun=1&asp_id=ASP00001&simin_yn=N"
        ),
        "issue_label": "서울시평생학습포털에서 수강하기",
    },
    {
        "key": "criminal_record",
        "label": "범죄경력회보서",
        "desc": "경찰청 범죄경력회보서 발급시스템에서 직접 발급받으실 수 있습니다. "
                "아동 관련 기관 취업 목적으로 신청하세요.",
        "required": True,
        "issue_url": "https://crims.police.go.kr/main.do",
        "issue_label": "범죄경력회보서 발급 사이트",
        "agency": True,
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
AGENCY_DOC_KEYS = {d["key"] for d in DOC_TYPES if d.get("agency")}

# 다이음 다이렉트 발급 대행
AGENCY_FEE = 5000
AGENCY_FEE_NOTICE = (
    "발급 절차 진행 수수료 5,000원이 친정엄마 급여에 추가되어 지급됩니다."
)
AGENCY_NOTICE = (
    "본인 발급이 어려우시면 다이음 다이렉트가 대신 발급해 드립니다. "
    "휴대폰 본인인증이 필요해 담당자가 전화를 드려야 하므로, 연락 가능한 시간을 알려주세요."
)
AGENCY_STATUS_LABELS = {
    "requested": "발급 신청 접수",
    "in_progress": "발급 진행중",
    "done": "발급 완료",
    "canceled": "취소",
}
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

# 바우처 구분코드 — 보건소 방문 또는 복지로에서 신청하면 받는 코드
VOUCHER_CODE_EXAMPLE = "A-통합-1형"
VOUCHER_CODE_HELP = (
    "보건소를 방문하시거나 복지로(bokjiro.go.kr)에서 신청하시면 받는 코드입니다. "
    f"예: {VOUCHER_CODE_EXAMPLE} · 아직 모르시면 비워두고 넘어가셔도 됩니다."
)

# 다이음 다이렉트를 알게 된 경로 — 어디에 홍보를 더 할지 판단하는 근거가 된다.
REFERRAL_OPTIONS = [
    "맘카페·온라인 커뮤니티",
    "인터넷 검색",
    "지인 소개",
    "인스타그램·SNS",
    "유튜브",
    "산부인과·산후조리원",
    "보건소",
    "기타",
]

# 산후조리원 이용
CENTER_USE_OPTIONS = ["이용함", "이용 안 함"]
CENTER_PERIOD_OPTIONS = ["1주", "2주", "3주", "4주 이상", "미정"]

# 개인정보 동의 항목. version 은 문구가 바뀔 때 올린다 — 어떤 버전에 동의했는지 남겨야
# 나중에 "무엇에 동의했는가"를 증명할 수 있다. 실제 오픈 전 법률 검토 필요.
CONSENT_VERSION = "2026-08-30"
CONSENTS: list[dict] = [
    {
        "key": "privacy",
        "required": True,
        "label": "개인정보 수집·이용 동의",
        "purpose": "산모신생아건강관리 서비스 신청 접수, 자격 확인, 상담 및 안내",
        "items": "산모 이름·연락처, 산후도우미 이름·연락처, 산모와의 관계, 출산예정일, 이용예정기간, 보건소 지정 이용코드",
        "period": "서비스 종료 후 3년 (관계 법령에 따른 보존기간이 더 길면 그 기간)",
        "note": "동의를 거부하실 수 있으나, 거부 시 신청 접수가 불가능합니다.",
    },
    {
        "key": "sensitive",
        "required": True,
        "label": "민감정보(건강정보) 처리 동의",
        "purpose": "산모신생아건강관리사 자격 요건 확인",
        "items": "건강검진결과서(결핵 검사 포함), 백일해 예방접종 증명서류, 교육 수료증",
        "period": "서비스 종료 후 3년",
        "note": "건강에 관한 정보는 민감정보로 별도 동의가 필요합니다. 거부 시 서류 검토가 불가능합니다.",
    },
    {
        "key": "kakao",
        "required": True,
        "label": "카카오 알림톡 수신 및 채널 친구추가 동의",
        "purpose": "접수·검토·보완요청·최종확인 등 진행 상황 안내",
        "items": "휴대전화번호",
        "period": "서비스 종료 시까지",
        "note": "본 서비스의 모든 안내는 카카오 알림톡으로 발송되므로, 채널 친구추가와 유지가 필요합니다.",
    },
    {
        "key": "marketing",
        "required": False,
        "label": "이벤트·혜택 안내 수신 동의 (선택)",
        "purpose": "월별 이벤트, 급여 변동, 신규 서비스 안내",
        "items": "휴대전화번호",
        "period": "동의 철회 시까지",
        "note": "동의하지 않으셔도 신청과 서비스 이용에는 영향이 없습니다.",
    },
]
CONSENT_KEYS = [c["key"] for c in CONSENTS]
REQUIRED_CONSENTS = [c["key"] for c in CONSENTS if c["required"]]

# 메인 화면 브랜드·카카오 채널 설정 (관리자가 수정)
DEFAULT_BRAND = {
    "service_name": "다이음 다이렉트",
    "tagline": "직접 신청해서, 친정엄마 급여를 가장 높게",
    "channel_name": "다이음",
    "channel_url": "",
    "event_notice": "이벤트 혜택은 다이음 채널을 친구추가하신 분께 적용됩니다.",
    "updated_at": "",
}

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
    voucher_code       TEXT NOT NULL DEFAULT '',
    center_use         TEXT NOT NULL DEFAULT '',
    center_period      TEXT NOT NULL DEFAULT '',
    referral           TEXT NOT NULL DEFAULT '',
    referral_detail    TEXT NOT NULL DEFAULT '',
    memo               TEXT NOT NULL DEFAULT '',
    consents           TEXT NOT NULL DEFAULT '{}',
    consent_at         TEXT NOT NULL DEFAULT '',
    consent_version    TEXT NOT NULL DEFAULT '',
    kakao_friend       INTEGER NOT NULL DEFAULT 0,
    kakao_verified     TEXT NOT NULL DEFAULT '',
    event_applied      INTEGER NOT NULL DEFAULT 0,
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
    drive_url      TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS issue_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    doc_type       TEXT NOT NULL,
    contact_time   TEXT NOT NULL DEFAULT '',
    memo           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'requested',
    admin_note     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_issue_app ON issue_requests(application_id);

CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    template_key   TEXT NOT NULL,
    phone          TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    link           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'queued',
    detail         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    sent_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_noti_app ON notifications(application_id);

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


# 이미 만들어진 DB에 나중에 추가된 컬럼 — 있으면 넘어간다.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("documents", "drive_url", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "consents", "TEXT NOT NULL DEFAULT '{}'"),
    ("applications", "consent_at", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "consent_version", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "kakao_friend", "INTEGER NOT NULL DEFAULT 0"),
    ("applications", "kakao_verified", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "event_applied", "INTEGER NOT NULL DEFAULT 0"),
    ("notifications", "link", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "center_use", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "center_period", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "referral", "TEXT NOT NULL DEFAULT ''"),
    ("applications", "referral_detail", "TEXT NOT NULL DEFAULT ''"),
]

# 이름이 바뀐 컬럼 — 기존 DB 의 값을 그대로 옮긴다.
RENAMED_COLUMNS: list[tuple[str, str, str]] = [
    ("applications", "health_center_code", "voucher_code"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, old, new in RENAMED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if old in cols and new not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    for table, column, decl in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, default in (("pay", DEFAULT_PAY), ("brand", DEFAULT_BRAND)):
            if conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?)",
                    (key, json.dumps(default, ensure_ascii=False)),
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
