"""친정엄마 산모신생아건강관리사 온라인 신청·서류 접수 시스템.

흐름
    1) 신청자는 접수 전에 필요한 서류·입력 항목을 메인에서 미리 확인한다.
    2) 신청서를 작성해 접수하면 자동 승인(접수완료)된다. 서류가 하나도 없어도 접수된다.
    3) 서류는 접수 전/후 언제든 올릴 수 있고, 접수하지 않은 채 임시저장만 해둘 수도 있다.
    4) 다이음 관리자가 서류를 확인해 부족분을 메시지와 함께 반려하면,
       신청자는 반려 내용을 보고 부족한 서류만 추가로 올려 다시 제출한다.
    5) 접수 → 검토 → 최종확인 단계는 신청자(친정엄마 산후도우미)도 조회 페이지에서 본다.

실행 (care/ 에서):
    uvicorn server.app:app --host 0.0.0.0 --port 8000

환경변수:
    ADMIN_PASSWORD  관리자 API 비밀번호 (필수 — 없으면 관리자 기능 비활성화)
    DATA_DIR        DB·업로드 저장 경로 (기본 care/data)
    MAX_UPLOAD_MB   업로드 1건 최대 크기 (기본 12)
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, gsync, notify

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "12")) * 1024 * 1024
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="친정엄마 산후도우미 신청 시스템", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── 인증·유틸 ────────────────────────────────────────────────

def require_admin(x_admin_password: str | None = Header(default=None)) -> str:
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "서버에 ADMIN_PASSWORD 가 설정되지 않아 관리자 기능이 비활성화되어 있습니다.")
    if not x_admin_password or not hmac.compare_digest(x_admin_password, expected):
        raise HTTPException(401, "관리자 비밀번호가 올바르지 않습니다.")
    return "admin"


_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)


def _throttle(key: str, limit: int, window: int = 300) -> None:
    """조회·신청 남용 방지용 간단한 IP 단위 제한(서버 1대 전제)."""
    bucket = _ATTEMPTS[key]
    cutoff = time.time() - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    bucket.append(time.time())


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "-")


def _row_to_app(row) -> dict:
    d = dict(row)
    d.pop("token", None)
    try:
        d["missing_docs"] = json.loads(d.get("missing_docs") or "[]")
    except json.JSONDecodeError:
        d["missing_docs"] = []
    d["missing_doc_labels"] = [db.DOC_LABELS.get(k, k) for k in d["missing_docs"]]
    try:
        d["consents"] = json.loads(d.get("consents") or "{}")
    except json.JSONDecodeError:
        d["consents"] = {}
    d["kakao_friend"] = bool(d.get("kakao_friend"))
    d["event_applied"] = bool(d.get("event_applied"))
    d["status_label"] = db.STATUS_LABELS.get(d["status"], d["status"])
    d["step"] = db.STATUS_ORDER.index(d["status"]) if d["status"] in db.STATUS_ORDER else 1
    return d


def _docs_of(conn, app_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, doc_type, filename, content_type, size, status, reject_reason, drive_url,"
        " uploaded_at FROM documents WHERE application_id=? ORDER BY uploaded_at",
        (app_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["doc_label"] = db.DOC_LABELS.get(d["doc_type"], d["doc_type"])
        out.append(d)
    return out


def _events_of(conn, app_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, message, actor, created_at FROM events WHERE application_id=? ORDER BY id",
        (app_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _missing_doc_types(docs: list[dict]) -> list[str]:
    """반려되지 않은 유효 서류가 하나도 없는 필수 서류 종류."""
    ok = {d["doc_type"] for d in docs if d["status"] != "rejected"}
    return [t["key"] for t in db.DOC_TYPES if t["required"] and t["key"] not in ok]


def _fetch_by_token(conn, code: str, token: str):
    row = conn.execute("SELECT * FROM applications WHERE code=?", (code.strip().upper(),)).fetchone()
    if row is None or not token or not hmac.compare_digest(row["token"], token):
        raise HTTPException(404, "신청 내역을 찾을 수 없습니다. 접수번호와 조회코드를 확인해 주세요.")
    return row


def _detail(conn, row) -> dict:
    data = _row_to_app(row)
    docs = _docs_of(conn, row["id"])
    data["documents"] = docs
    data["missing_now"] = [db.DOC_LABELS[k] for k in _missing_doc_types(docs)]
    data["events"] = _events_of(conn, row["id"])
    return data


# ── 신청 입력 검증 ───────────────────────────────────────────

class ApplicationIn(BaseModel):
    mother_name: str = Field(default="", max_length=40)
    mother_phone: str = Field(default="", max_length=30)
    helper_name: str = Field(default="", max_length=40)
    helper_phone: str = Field(default="", max_length=30)
    helper_relation: str = Field(default="가족", max_length=20)
    relation_detail: str = Field(default="", max_length=40)
    due_date: str = Field(default="", max_length=20)
    service_days: int = db.DEFAULT_SERVICE_DAYS
    health_center_code: str = Field(default="", max_length=40)
    memo: str = Field(default="", max_length=1000)
    consents: dict[str, bool] = Field(default_factory=dict)  # 개인정보·민감정보·알림톡 동의
    kakao_friend: bool = False                               # 카카오채널 친구추가 확인
    submit: bool = False  # True 면 저장과 동시에 접수(자동승인)


def _clean(payload: ApplicationIn) -> dict:
    days = payload.service_days if payload.service_days in db.SERVICE_DAY_OPTIONS else db.DEFAULT_SERVICE_DAYS
    relation = payload.helper_relation if payload.helper_relation in ("가족", "지인") else "가족"
    due = payload.due_date.strip()
    if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise HTTPException(400, "출산예정일은 YYYY-MM-DD 형식으로 입력해 주세요.")
    return {
        "mother_name": payload.mother_name.strip(),
        "mother_phone": db.normalize_phone(payload.mother_phone),
        "helper_name": payload.helper_name.strip(),
        "helper_phone": db.normalize_phone(payload.helper_phone),
        "helper_relation": relation,
        "relation_detail": payload.relation_detail.strip(),
        "due_date": due,
        "service_days": days,
        "health_center_code": payload.health_center_code.strip(),
        "memo": payload.memo.strip(),
        "consents": {k: bool(payload.consents.get(k)) for k in db.CONSENT_KEYS},
        "kakao_friend": 1 if payload.kakao_friend else 0,
    }


REQUIRED_FIELDS = [
    ("mother_name", "산모 이름"),
    ("mother_phone", "산모 연락처"),
    ("helper_name", "산후도우미 이름"),
    ("helper_phone", "산후도우미 연락처"),
    ("due_date", "출산예정일"),
]


def _check_submittable(fields: dict) -> None:
    missing = [label for key, label in REQUIRED_FIELDS if not fields.get(key)]
    if missing:
        raise HTTPException(400, "접수하려면 다음 항목이 필요합니다: " + ", ".join(missing))

    consents = fields.get("consents") or {}
    if isinstance(consents, str):
        try:
            consents = json.loads(consents)
        except json.JSONDecodeError:
            consents = {}
    not_agreed = [c["label"] for c in db.CONSENTS if c["required"] and not consents.get(c["key"])]
    if not_agreed:
        raise HTTPException(400, "다음 항목에 동의해야 접수할 수 있습니다: " + ", ".join(not_agreed))
    if not fields.get("kakao_friend"):
        raise HTTPException(
            400,
            "다이음 카카오채널 친구추가가 필요합니다. 모든 진행 안내가 알림톡으로 발송되며, "
            "친구추가를 하지 않으면 이벤트 혜택도 적용되지 않습니다.",
        )


# ── 공개 API ────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "admin_enabled": bool(os.environ.get("ADMIN_PASSWORD"))}


@app.get("/api/info")
def info():
    """접수 전에 확인하는 안내: 필요한 서류, 입력 항목, 이번 달 친정엄마 소득."""
    with db.db() as conn:
        pay = db.get_setting(conn, "pay", db.DEFAULT_PAY)
        brand = db.get_setting(conn, "brand", db.DEFAULT_BRAND)
    return {
        "brand": brand,
        "consents": db.CONSENTS,
        "consent_version": db.CONSENT_VERSION,
        "documents": db.DOC_TYPES,
        "fields": [
            {"key": k, "label": label, "required": True} for k, label in REQUIRED_FIELDS
        ]
        + [
            {"key": "helper_relation", "label": "산모와의 관계(가족/지인)", "required": True},
            {"key": "service_days", "label": "이용예정기간", "required": True,
             "options": db.SERVICE_DAY_OPTIONS, "default": db.DEFAULT_SERVICE_DAYS},
            {"key": "health_center_code", "label": "보건소 지정 최종이용코드", "required": False,
             "note": "모르면 비워두고 넘어가도 됩니다. 나중에 추가 입력할 수 있습니다."},
        ],
        "service_days_options": db.SERVICE_DAY_OPTIONS,
        "service_days_default": db.DEFAULT_SERVICE_DAYS,
        "statuses": db.STATUS_LABELS,
        "pay": pay,
    }


@app.post("/api/applications")
def create_application(payload: ApplicationIn, request: Request):
    _throttle(f"create:{_client_ip(request)}", limit=20)
    fields = _clean(payload)
    if payload.submit:
        _check_submittable(fields)
    with db.db() as conn:
        code = db.new_code(conn)
        token = db.new_token()
        ts = db.now()
        status = "received" if payload.submit else "draft"
        agreed = any(fields["consents"].values())
        cur = conn.execute(
            "INSERT INTO applications(code, token, mother_name, mother_phone, helper_name, helper_phone,"
            " helper_relation, relation_detail, due_date, service_days, health_center_code, memo,"
            " consents, consent_at, consent_version, kakao_friend, event_applied,"
            " status, created_at, updated_at, submitted_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code, token, fields["mother_name"], fields["mother_phone"], fields["helper_name"],
                fields["helper_phone"], fields["helper_relation"], fields["relation_detail"],
                fields["due_date"], fields["service_days"], fields["health_center_code"], fields["memo"],
                json.dumps(fields["consents"], ensure_ascii=False), ts if agreed else "",
                db.CONSENT_VERSION if agreed else "", fields["kakao_friend"], fields["kakao_friend"],
                status, ts, ts, ts if payload.submit else "",
            ),
        )
        app_id = int(cur.lastrowid)
        db.add_event(conn, app_id, "created", "신청서가 작성되었습니다.")
        notif = None
        if payload.submit:
            db.add_event(conn, app_id, "received", "접수가 자동 승인되었습니다. 서류 검토 전까지 서류를 추가할 수 있습니다.")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        data = _detail(conn, row)
        if payload.submit:
            notif = notify.queue_status_change(conn, dict(row), data["missing_now"])
    notify.dispatch(notif)
    gsync.queue_application(code)
    data["token"] = token
    return data


@app.get("/api/applications/{code}")
def get_application(code: str, token: str = Query(default="")):
    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        return _detail(conn, row)


class LookupIn(BaseModel):
    code: str = Field(default="", max_length=40)
    phone: str = Field(default="", max_length=30)


@app.post("/api/applications/lookup")
def lookup(payload: LookupIn, request: Request):
    """접수번호 + 산모 연락처로 조회코드(토큰)를 되찾는다."""
    _throttle(f"lookup:{_client_ip(request)}", limit=12)
    code = payload.code.strip().upper()
    digits = db.phone_digits(payload.phone)
    if not code or not digits:
        raise HTTPException(400, "접수번호와 산모 연락처를 모두 입력해 주세요.")
    with db.db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code=?", (code,)).fetchone()
        if row is None or db.phone_digits(row["mother_phone"]) != digits:
            raise HTTPException(404, "일치하는 신청 내역이 없습니다. 접수번호와 연락처를 확인해 주세요.")
        data = _detail(conn, row)
        data["token"] = row["token"]
        return data


@app.patch("/api/applications/{code}")
def update_application(code: str, payload: ApplicationIn, token: str = Query(default="")):
    fields = _clean(payload)
    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        if row["status"] == "confirmed":
            raise HTTPException(409, "최종확인이 완료된 신청서는 수정할 수 없습니다. 다이음으로 문의해 주세요.")
        agreed = any(fields["consents"].values())
        conn.execute(
            "UPDATE applications SET mother_name=?, mother_phone=?, helper_name=?, helper_phone=?,"
            " helper_relation=?, relation_detail=?, due_date=?, service_days=?, health_center_code=?,"
            " memo=?, consents=?, consent_at=?, consent_version=?, kakao_friend=?, updated_at=?"
            " WHERE id=?",
            (
                fields["mother_name"], fields["mother_phone"], fields["helper_name"], fields["helper_phone"],
                fields["helper_relation"], fields["relation_detail"], fields["due_date"],
                fields["service_days"], fields["health_center_code"], fields["memo"],
                json.dumps(fields["consents"], ensure_ascii=False),
                db.now() if agreed else row["consent_at"],
                db.CONSENT_VERSION if agreed else row["consent_version"],
                fields["kakao_friend"], db.now(), row["id"],
            ),
        )
        db.add_event(conn, row["id"], "updated", "신청 정보가 수정되었습니다.", actor="신청자")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
    gsync.queue_application(row["code"])
    return detail


@app.post("/api/applications/{code}/submit")
def submit_application(code: str, token: str = Query(default="")):
    """임시저장분을 접수하거나(자동승인), 반려된 건을 보완해 다시 제출한다."""
    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        if row["status"] == "confirmed":
            raise HTTPException(409, "이미 최종확인이 완료된 신청입니다.")
        _check_submittable(dict(row))
        resubmit = row["status"] == "rejected"
        conn.execute(
            "UPDATE applications SET status='received', admin_message='', missing_docs='[]',"
            " submitted_at=?, updated_at=? WHERE id=?",
            (row["submitted_at"] or db.now(), db.now(), row["id"]),
        )
        db.add_event(
            conn, row["id"], "received",
            "보완서류를 다시 제출했습니다. 재검토 대기중입니다." if resubmit
            else "접수가 자동 승인되었습니다. 서류 검토 전까지 서류를 추가할 수 있습니다.",
            actor="신청자",
        )
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
        notif = notify.queue_status_change(conn, dict(row), detail["missing_now"])
    notify.dispatch(notif)
    gsync.queue_application(row["code"])
    return detail


# ── 서류 업로드 ──────────────────────────────────────────────

@app.post("/api/applications/{code}/documents")
async def upload_document(
    code: str,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    token: str = Query(default=""),
):
    if doc_type not in db.DOC_TYPE_KEYS:
        raise HTTPException(400, "알 수 없는 서류 종류입니다.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "PDF 또는 이미지(JPG·PNG·WEBP·HEIC) 파일만 올릴 수 있습니다.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "빈 파일입니다.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"파일이 너무 큽니다. {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 이하로 올려 주세요.")

    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        if row["status"] == "confirmed":
            raise HTTPException(409, "최종확인이 완료되어 서류를 추가할 수 없습니다.")
        stored = f"{uuid.uuid4().hex}{ext}"
        target_dir = db.UPLOAD_DIR / row["code"]
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / stored).write_bytes(content)
        cur = conn.execute(
            "INSERT INTO documents(application_id, doc_type, filename, stored_name, content_type, size,"
            " uploaded_at) VALUES(?,?,?,?,?,?,?)",
            (
                row["id"], doc_type, Path(file.filename or stored).name, stored,
                file.content_type or "", len(content), db.now(),
            ),
        )
        doc_id = int(cur.lastrowid)
        db.add_event(conn, row["id"], "document", f"{db.DOC_LABELS[doc_type]} 서류를 올렸습니다.", actor="신청자")
        conn.execute("UPDATE applications SET updated_at=? WHERE id=?", (db.now(), row["id"]))
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
    gsync.queue_document(doc_id)      # 드라이브 업로드 후 신청 행까지 갱신된다
    gsync.queue_application(row["code"])
    return detail


@app.delete("/api/applications/{code}/documents/{doc_id}")
def delete_document(code: str, doc_id: int, token: str = Query(default="")):
    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        if row["status"] == "confirmed":
            raise HTTPException(409, "최종확인이 완료되어 서류를 삭제할 수 없습니다.")
        doc = conn.execute(
            "SELECT * FROM documents WHERE id=? AND application_id=?", (doc_id, row["id"])
        ).fetchone()
        if doc is None:
            raise HTTPException(404, "서류를 찾을 수 없습니다.")
        (db.UPLOAD_DIR / row["code"] / doc["stored_name"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        db.add_event(conn, row["id"], "document", f"{db.DOC_LABELS.get(doc['doc_type'], '')} 서류를 삭제했습니다.", actor="신청자")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
    gsync.queue_application(row["code"])
    return detail


def _serve_document(conn, row, doc_id: int) -> Response:
    doc = conn.execute(
        "SELECT * FROM documents WHERE id=? AND application_id=?", (doc_id, row["id"])
    ).fetchone()
    if doc is None:
        raise HTTPException(404, "서류를 찾을 수 없습니다.")
    path = db.UPLOAD_DIR / row["code"] / doc["stored_name"]
    if not path.exists():
        raise HTTPException(404, "저장된 파일을 찾을 수 없습니다.")
    media = doc["content_type"] or mimetypes.guess_type(doc["filename"])[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        headers={
            # 개인정보·건강정보이므로 중간 캐시에 남기지 않는다.
            "Cache-Control": "no-store, private",
            "Content-Disposition": f'inline; filename="{uuid.uuid4().hex}{Path(doc["filename"]).suffix}"',
        },
    )


@app.get("/api/applications/{code}/documents/{doc_id}/file")
def get_document_file(code: str, doc_id: int, token: str = Query(default="")):
    with db.db() as conn:
        row = _fetch_by_token(conn, code, token)
        return _serve_document(conn, row, doc_id)


# ── 전화상담 신청 ────────────────────────────────────────────

class ConsultationIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    phone: str = Field(min_length=1, max_length=30)
    preferred_time: str = Field(default="", max_length=60)
    memo: str = Field(default="", max_length=500)


@app.post("/api/consultations")
def create_consultation(payload: ConsultationIn, request: Request):
    _throttle(f"consult:{_client_ip(request)}", limit=10)
    if not db.phone_digits(payload.phone):
        raise HTTPException(400, "연락처를 정확히 입력해 주세요.")
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO consultations(name, phone, preferred_time, memo, created_at) VALUES(?,?,?,?,?)",
            (
                payload.name.strip(), db.normalize_phone(payload.phone),
                payload.preferred_time.strip(), payload.memo.strip(), db.now(),
            ),
        )
        cid = int(cur.lastrowid)
        c = conn.execute("SELECT * FROM consultations WHERE id=?", (cid,)).fetchone()
        notif = notify.queue_consultation(conn, dict(c))
    notify.dispatch(notif)
    gsync.queue_consultation(cid)
    return {"ok": True, "message": "전화상담 신청이 접수되었습니다. 담당자가 순차적으로 연락드립니다."}


# ── 관리자 API ───────────────────────────────────────────────

@app.get("/api/admin/applications")
def admin_list(status: str = Query(default=""), q: str = Query(default=""), _: str = Depends(require_admin)):
    sql = "SELECT * FROM applications"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if q.strip():
        where.append("(code LIKE ? OR mother_name LIKE ? OR helper_name LIKE ? OR mother_phone LIKE ?)")
        args += [f"%{q.strip()}%"] * 4
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 300"
    with db.db() as conn:
        rows = conn.execute(sql, args).fetchall()
        items = []
        for row in rows:
            data = _row_to_app(row)
            docs = _docs_of(conn, row["id"])
            data["doc_count"] = len(docs)
            data["missing_now"] = [db.DOC_LABELS[k] for k in _missing_doc_types(docs)]
            items.append(data)
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
        }
    return {"items": items, "counts": counts, "statuses": db.STATUS_LABELS}


@app.get("/api/admin/applications/{code}")
def admin_detail(code: str, _: str = Depends(require_admin)):
    with db.db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code=?", (code.strip().upper(),)).fetchone()
        if row is None:
            raise HTTPException(404, "신청 내역을 찾을 수 없습니다.")
        return _detail(conn, row)


@app.get("/api/admin/applications/{code}/documents/{doc_id}/file")
def admin_document_file(code: str, doc_id: int, _: str = Depends(require_admin)):
    with db.db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code=?", (code.strip().upper(),)).fetchone()
        if row is None:
            raise HTTPException(404, "신청 내역을 찾을 수 없습니다.")
        return _serve_document(conn, row, doc_id)


class StatusIn(BaseModel):
    status: str
    message: str = Field(default="", max_length=1000)
    missing_docs: list[str] = Field(default_factory=list)


@app.post("/api/admin/applications/{code}/status")
def admin_set_status(code: str, payload: StatusIn, _: str = Depends(require_admin)):
    """접수/검토/최종확인 단계 변경, 또는 부족 서류를 지정해 반려."""
    if payload.status not in db.STATUS_LABELS or payload.status == "draft":
        raise HTTPException(400, "변경할 수 없는 상태입니다.")
    missing = [k for k in payload.missing_docs if k in db.DOC_TYPE_KEYS]
    if payload.status == "rejected" and not (missing or payload.message.strip()):
        raise HTTPException(400, "반려할 때는 부족한 서류를 선택하거나 사유를 입력해 주세요.")
    with db.db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code=?", (code.strip().upper(),)).fetchone()
        if row is None:
            raise HTTPException(404, "신청 내역을 찾을 수 없습니다.")
        confirmed_at = db.now() if payload.status == "confirmed" else ""
        conn.execute(
            "UPDATE applications SET status=?, admin_message=?, missing_docs=?, confirmed_at=?, updated_at=?"
            " WHERE id=?",
            (
                payload.status, payload.message.strip(), json.dumps(missing, ensure_ascii=False),
                confirmed_at, db.now(), row["id"],
            ),
        )
        if payload.status == "rejected":
            labels = ", ".join(db.DOC_LABELS[k] for k in missing)
            text = "보완요청: " + (f"부족 서류 — {labels}. " if labels else "")
            text += payload.message.strip()
        else:
            text = f"{db.STATUS_LABELS[payload.status]}" + (f" — {payload.message.strip()}" if payload.message.strip() else "")
        db.add_event(conn, row["id"], payload.status, text.strip(), actor="다이음 관리자")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
        labels = [db.DOC_LABELS[k] for k in missing] or detail["missing_now"]
        notif = notify.queue_status_change(conn, dict(row), labels)
    notify.dispatch(notif)
    gsync.queue_application(row["code"])
    return detail


class DocReviewIn(BaseModel):
    status: str  # accepted | rejected | uploaded
    reason: str = Field(default="", max_length=300)


@app.post("/api/admin/documents/{doc_id}/review")
def admin_review_document(doc_id: int, payload: DocReviewIn, _: str = Depends(require_admin)):
    if payload.status not in ("accepted", "rejected", "uploaded"):
        raise HTTPException(400, "알 수 없는 서류 상태입니다.")
    with db.db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "서류를 찾을 수 없습니다.")
        conn.execute(
            "UPDATE documents SET status=?, reject_reason=? WHERE id=?",
            (payload.status, payload.reason.strip(), doc_id),
        )
        label = db.DOC_LABELS.get(doc["doc_type"], doc["doc_type"])
        verb = {"accepted": "확인 완료", "rejected": "반려", "uploaded": "확인 대기로 되돌림"}[payload.status]
        message = f"{label} — {verb}" + (f": {payload.reason.strip()}" if payload.reason.strip() else "")
        db.add_event(conn, doc["application_id"], "document_review", message, actor="다이음 관리자")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (doc["application_id"],)).fetchone()
        detail = _detail(conn, row)
    gsync.queue_application(row["code"])
    return detail


class PayIn(BaseModel):
    effective_month: str = Field(default="", max_length=20)
    hourly_wage: int = 0
    headline: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=600)
    rows: list[dict] = Field(default_factory=list)


@app.get("/api/admin/pay")
def admin_get_pay(_: str = Depends(require_admin)):
    with db.db() as conn:
        return db.get_setting(conn, "pay", db.DEFAULT_PAY)


@app.put("/api/admin/pay")
def admin_set_pay(payload: PayIn, _: str = Depends(require_admin)):
    """메인 화면에 노출되는 친정엄마 소득 안내를 갱신한다."""
    rows = []
    for raw in payload.rows[:12]:
        label = str(raw.get("label", "")).strip()[:40]
        if not label:
            continue
        try:
            amount = int(str(raw.get("amount", 0)).replace(",", "") or 0)
        except ValueError:
            amount = 0
        rows.append({"label": label, "amount": amount, "note": str(raw.get("note", "")).strip()[:80]})
    value = {
        "effective_month": payload.effective_month.strip(),
        "hourly_wage": max(0, payload.hourly_wage),
        "headline": payload.headline.strip() or db.DEFAULT_PAY["headline"],
        "note": payload.note.strip(),
        "rows": rows,
        "updated_at": db.now(),
    }
    with db.db() as conn:
        db.set_setting(conn, "pay", value)
    return value


class BrandIn(BaseModel):
    service_name: str = Field(default="", max_length=60)
    tagline: str = Field(default="", max_length=120)
    channel_name: str = Field(default="", max_length=40)
    channel_url: str = Field(default="", max_length=300)
    event_notice: str = Field(default="", max_length=400)


@app.get("/api/admin/brand")
def admin_get_brand(_: str = Depends(require_admin)):
    with db.db() as conn:
        return db.get_setting(conn, "brand", db.DEFAULT_BRAND)


@app.put("/api/admin/brand")
def admin_set_brand(payload: BrandIn, _: str = Depends(require_admin)):
    """서비스명·홍보문구·카카오채널 주소. 메인 화면과 신청서에 바로 반영된다."""
    url = payload.channel_url.strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "카카오채널 주소는 http:// 또는 https:// 로 시작해야 합니다.")
    value = {
        "service_name": payload.service_name.strip() or db.DEFAULT_BRAND["service_name"],
        "tagline": payload.tagline.strip(),
        "channel_name": payload.channel_name.strip() or db.DEFAULT_BRAND["channel_name"],
        "channel_url": url,
        "event_notice": payload.event_notice.strip(),
        "updated_at": db.now(),
    }
    with db.db() as conn:
        db.set_setting(conn, "brand", value)
    return value


class KakaoIn(BaseModel):
    verified: str = ""          # '' | 'yes' | 'no'
    event_applied: bool = True


@app.post("/api/admin/applications/{code}/kakao")
def admin_set_kakao(code: str, payload: KakaoIn, _: str = Depends(require_admin)):
    """채널 친구추가 확인 결과와 이벤트 적용 여부를 기록한다.

    이벤트는 친구추가 후 1년 유지가 조건이므로, 확인 결과를 남겨 근거로 삼는다.
    """
    if payload.verified not in ("", "yes", "no"):
        raise HTTPException(400, "알 수 없는 확인 결과입니다.")
    with db.db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code=?", (code.strip().upper(),)).fetchone()
        if row is None:
            raise HTTPException(404, "신청 내역을 찾을 수 없습니다.")
        applied = 1 if (payload.event_applied and payload.verified != "no") else 0
        conn.execute(
            "UPDATE applications SET kakao_verified=?, event_applied=?, updated_at=? WHERE id=?",
            (payload.verified, applied, db.now(), row["id"]),
        )
        text = {"yes": "채널 친구추가 확인됨", "no": "채널 친구추가 미확인 — 이벤트 미적용",
                "": "채널 친구추가 확인 대기"}[payload.verified]
        db.add_event(conn, row["id"], "kakao", text, actor="다이음 관리자")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        detail = _detail(conn, row)
    gsync.queue_application(row["code"])
    return detail


@app.get("/api/admin/notifications")
def admin_notifications(code: str = Query(default=""), _: str = Depends(require_admin)):
    """알림톡 발송 이력. code 를 주면 그 신청 건만."""
    sql = ("SELECT n.*, a.code AS app_code FROM notifications n"
           " LEFT JOIN applications a ON a.id = n.application_id")
    args: list = []
    if code.strip():
        sql += " WHERE a.code = ?"
        args.append(code.strip().upper())
    sql += " ORDER BY n.id DESC LIMIT 200"
    with db.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["template_title"] = notify.TEMPLATES.get(r["template_key"], {}).get("title", r["template_key"])
    return {"items": rows, **notify.status()}


@app.post("/api/admin/notifications/{nid}/resend")
def admin_resend(nid: int, _: str = Depends(require_admin)):
    with db.db() as conn:
        if conn.execute("SELECT 1 FROM notifications WHERE id=?", (nid,)).fetchone() is None:
            raise HTTPException(404, "발송 내역을 찾을 수 없습니다.")
    notify.resend(nid)
    return {"ok": True}


@app.get("/api/admin/sync")
def admin_sync_status(_: str = Depends(require_admin)):
    """구글 시트·드라이브 연동 상태."""
    return gsync.status()


@app.post("/api/admin/sync")
def admin_sync_all(_: str = Depends(require_admin)):
    """DB 전체를 시트로 다시 밀어 넣는다 (연동을 나중에 켰거나 시트를 새로 만든 경우)."""
    try:
        queued = gsync.sync_all()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "queued": queued, **gsync.status()}


@app.get("/api/admin/consultations")
def admin_consultations(_: str = Depends(require_admin)):
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM consultations ORDER BY id DESC LIMIT 300").fetchall()
        return {"items": [dict(r) for r in rows]}


class ConsultUpdateIn(BaseModel):
    status: str = "done"
    admin_note: str = Field(default="", max_length=500)


@app.post("/api/admin/consultations/{cid}")
def admin_update_consultation(cid: int, payload: ConsultUpdateIn, _: str = Depends(require_admin)):
    if payload.status not in ("new", "done"):
        raise HTTPException(400, "알 수 없는 상태입니다.")
    with db.db() as conn:
        cur = conn.execute(
            "UPDATE consultations SET status=?, admin_note=? WHERE id=?",
            (payload.status, payload.admin_note.strip(), cid),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "상담 신청을 찾을 수 없습니다.")
    gsync.queue_consultation(cid)
    return {"ok": True}


# ── 정적 페이지 ──────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
