"""구글 시트·드라이브 미러링.

주 저장소는 어디까지나 SQLite다. 이 모듈은 신청 정보를 **구글 시트에**,
업로드한 서류 원본을 **구글 드라이브에** 복사해 두는 백업/조회용 사본을 만든다.
그래서 서버 디스크가 초기화돼도(예: Render free 플랜 재시작) 접수 내용과
서류가 남고, 관리자는 익숙한 시트에서 바로 확인·필터링할 수 있다.

동기화는 백그라운드 워커에서 처리하므로 구글이 느리거나 죽어도 신청·업로드
요청은 그대로 성공한다. 실패는 기록만 하고 재시도한다.

환경변수 (셋 다 없으면 기능 자체가 꺼진 채 동작한다):
    GOOGLE_SERVICE_ACCOUNT_JSON  서비스 계정 키 JSON 문자열(또는 파일 경로)
    GSHEET_ID                    스프레드시트 ID
    GDRIVE_FOLDER_ID             서류를 올릴 드라이브 폴더 ID (선택 — 없으면 시트만)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import db

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

APP_SHEET = "신청"
APP_HEADER = [
    "접수번호", "상태", "산모이름", "산모연락처", "산후도우미", "도우미연락처",
    "관계", "관계상세", "출산예정일", "이용예정기간", "산후조리원", "조리원기간",
    "바우처구분코드", "인지경로", "남긴말씀",
    "관리자메시지", "부족서류", "제출서류", "미제출서류", "서류링크",
    "접수일시", "최종확인일시", "최종수정",
]
CONSULT_SHEET = "전화상담"
CONSULT_HEADER = ["신청일시", "성함", "연락처", "통화희망시간", "문의내용", "상태", "관리자메모"]

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

_QUEUE: "queue.Queue[tuple[str, object]]" = queue.Queue()
_WORKER: threading.Thread | None = None
_LOCK = threading.Lock()
_CREDS = None
_STATE = {"last_ok": "", "last_error": "", "synced": 0, "failed": 0}


# ── 설정 ────────────────────────────────────────────────────

def _service_account_info() -> dict | None:
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    # 파일 경로로 줘도 되고, JSON 문자열을 그대로 넣어도 된다.
    if not raw.startswith("{"):
        path = Path(raw)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def sheet_id() -> str:
    return (os.environ.get("GSHEET_ID") or "").strip()


def folder_id() -> str:
    return (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()


def enabled() -> bool:
    return bool(_service_account_info() and sheet_id())


def sheet_url() -> str:
    sid = sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}" if sid else ""


# ── HTTP (테스트에서 이 한 곳만 대체하면 된다) ──────────────

def _token() -> str:
    global _CREDS
    from google.auth.transport.requests import Request  # 지연 임포트 — 미사용 시 의존성 불필요
    from google.oauth2 import service_account

    with _LOCK:
        if _CREDS is None:
            info = _service_account_info()
            if not info:
                raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 이 설정되지 않았습니다.")
            _CREDS = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        if not _CREDS.valid:
            _CREDS.refresh(Request())
        return _CREDS.token


def _request(method: str, url: str, *, params=None, json_body=None, data=None, headers=None) -> dict:
    import requests

    hdrs = {"Authorization": f"Bearer {_token()}"}
    hdrs.update(headers or {})
    res = requests.request(method, url, params=params, json=json_body, data=data,
                           headers=hdrs, timeout=30)
    if res.status_code >= 400:
        raise RuntimeError(f"구글 API 오류 {res.status_code}: {res.text[:300]}")
    return res.json() if res.content else {}


# ── 시트 조작 ────────────────────────────────────────────────

def _rng(title: str, a1: str) -> str:
    return urllib.parse.quote(f"'{title}'!{a1}", safe="")


def _ensure_sheet(title: str, header: list[str]) -> None:
    """탭이 없으면 만들고, 헤더 행이 비어 있으면 채운다."""
    meta = _request("GET", f"{SHEETS_API}/{sheet_id()}", params={"fields": "sheets.properties.title"})
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if title not in titles:
        _request("POST", f"{SHEETS_API}/{sheet_id()}:batchUpdate",
                 json_body={"requests": [{"addSheet": {"properties": {"title": title}}}]})
    first = _request("GET", f"{SHEETS_API}/{sheet_id()}/values/{_rng(title, 'A1:A1')}")
    if not first.get("values"):
        _request("PUT", f"{SHEETS_API}/{sheet_id()}/values/{_rng(title, 'A1')}",
                 params={"valueInputOption": "RAW"}, json_body={"values": [header]})


def _find_row(title: str, key: str) -> int | None:
    """A열에서 키(접수번호)를 찾아 1-based 행 번호를 돌려준다."""
    got = _request("GET", f"{SHEETS_API}/{sheet_id()}/values/{_rng(title, 'A:A')}")
    for i, row in enumerate(got.get("values", []), start=1):
        if row and row[0] == key:
            return i
    return None


def _upsert(title: str, header: list[str], key: str, row: list) -> None:
    _ensure_sheet(title, header)
    at = _find_row(title, key)
    if at:
        _request("PUT", f"{SHEETS_API}/{sheet_id()}/values/{_rng(title, f'A{at}')}",
                 params={"valueInputOption": "RAW"}, json_body={"values": [row]})
    else:
        _request("POST", f"{SHEETS_API}/{sheet_id()}/values/{_rng(title, 'A1')}:append",
                 params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                 json_body={"values": [row]})


# ── 행 만들기 ────────────────────────────────────────────────

def application_row(app: dict, docs: list[dict]) -> list:
    """신청 1건 → 시트 한 행. 컬럼 순서는 APP_HEADER 와 같다."""
    alive = [d for d in docs if d.get("status") != "rejected"]
    submitted = [db.DOC_LABELS.get(d["doc_type"], d["doc_type"]) for d in alive]
    have = {d["doc_type"] for d in alive}
    missing = [t["label"] for t in db.DOC_TYPES if t["required"] and t["key"] not in have]
    links = [d["drive_url"] for d in docs if d.get("drive_url")]
    days = app.get("service_days") or 0
    return [
        app.get("code", ""),
        db.STATUS_LABELS.get(app.get("status", ""), app.get("status", "")),
        app.get("mother_name", ""), app.get("mother_phone", ""),
        app.get("helper_name", ""), app.get("helper_phone", ""),
        app.get("helper_relation", ""), app.get("relation_detail", ""),
        app.get("due_date", ""),
        f"{days}일 이상" if days >= 25 else (f"{days}일" if days else ""),
        app.get("center_use", ""), app.get("center_period", ""),
        app.get("voucher_code", ""),
        (app.get("referral", "") + (f" ({app['referral_detail']})" if app.get("referral_detail") else "")),
        app.get("memo", ""),
        app.get("admin_message", ""),
        ", ".join(db.DOC_LABELS.get(k, k) for k in app.get("missing_docs", [])),
        ", ".join(dict.fromkeys(submitted)),
        ", ".join(missing),
        "\n".join(links),
        app.get("submitted_at", ""), app.get("confirmed_at", ""), app.get("updated_at", ""),
    ]


def consultation_row(c: dict) -> list:
    return [
        c.get("created_at", ""), c.get("name", ""), c.get("phone", ""),
        c.get("preferred_time", ""), c.get("memo", ""),
        "완료" if c.get("status") == "done" else "대기", c.get("admin_note", ""),
    ]


def _load_application(conn, code: str) -> tuple[dict, list[dict]] | None:
    row = conn.execute("SELECT * FROM applications WHERE code=?", (code,)).fetchone()
    if row is None:
        return None
    app = dict(row)
    app.pop("token", None)
    try:
        app["missing_docs"] = json.loads(app.get("missing_docs") or "[]")
    except json.JSONDecodeError:
        app["missing_docs"] = []
    docs = [dict(d) for d in conn.execute(
        "SELECT * FROM documents WHERE application_id=? ORDER BY uploaded_at", (row["id"],))]
    return app, docs


# ── 실제 동기화 작업 ─────────────────────────────────────────

def _push_application(code: str) -> None:
    with db.db() as conn:
        found = _load_application(conn, code)
    if not found:
        return
    app, docs = found
    _upsert(APP_SHEET, APP_HEADER, code, application_row(app, docs))


def _push_consultation(cid: int) -> None:
    with db.db() as conn:
        row = conn.execute("SELECT * FROM consultations WHERE id=?", (cid,)).fetchone()
    if row is None:
        return
    # 상담은 접수번호가 없으므로 "신청일시+연락처"를 키로 삼아 중복 없이 갱신한다.
    c = dict(row)
    key = c.get("created_at", "")
    _ensure_sheet(CONSULT_SHEET, CONSULT_HEADER)
    at = _find_row(CONSULT_SHEET, key)
    body = {"values": [consultation_row(c)]}
    if at:
        _request("PUT", f"{SHEETS_API}/{sheet_id()}/values/{_rng(CONSULT_SHEET, f'A{at}')}",
                 params={"valueInputOption": "RAW"}, json_body=body)
    else:
        _request("POST", f"{SHEETS_API}/{sheet_id()}/values/{_rng(CONSULT_SHEET, 'A1')}:append",
                 params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json_body=body)


def _push_document(doc_id: int) -> None:
    """서류 원본을 드라이브에 올리고 링크를 DB에 적은 뒤, 신청 행을 갱신한다."""
    if not folder_id():
        return
    with db.db() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if doc is None or doc["drive_url"]:
            return
        app = conn.execute("SELECT code FROM applications WHERE id=?", (doc["application_id"],)).fetchone()
        if app is None:
            return
        code = app["code"]
        label = db.DOC_LABELS.get(doc["doc_type"], doc["doc_type"])
        path = db.UPLOAD_DIR / code / doc["stored_name"]
        content_type = doc["content_type"] or "application/octet-stream"
        filename = doc["filename"]
    if not path.exists():
        return

    # 드라이브에서 한눈에 구분되도록 접수번호·서류명을 파일명에 넣는다.
    metadata = {"name": f"{code}__{label}__{filename}", "parents": [folder_id()]}
    boundary = "care-upload-boundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    created = _request(
        "POST", DRIVE_UPLOAD_API,
        params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,webViewLink"},
        data=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    link = created.get("webViewLink") or (
        f"https://drive.google.com/file/d/{created['id']}/view" if created.get("id") else "")
    if link:
        with db.db() as conn:
            conn.execute("UPDATE documents SET drive_url=? WHERE id=?", (link, doc_id))
    _push_application(code)


_HANDLERS = {
    "application": _push_application,
    "consultation": _push_consultation,
    "document": _push_document,
}


# ── 워커 ────────────────────────────────────────────────────

def _worker() -> None:
    while True:
        # get 과 task_done 은 반드시 같은 큐 객체여야 한다 (notify._worker 와 동일).
        q = _QUEUE
        kind, arg = q.get()
        try:
            if enabled():
                _run_with_retry(kind, arg)
        except Exception:  # 동기화 실패가 서비스에 영향을 주면 안 된다
            _STATE["failed"] += 1
            _STATE["last_error"] = f"[{db.now()}] {traceback.format_exc(limit=2).strip()[-300:]}"
            print(f"[gsync] {kind} 동기화 실패: {_STATE['last_error']}", flush=True)
        finally:
            q.task_done()


def _run_with_retry(kind: str, arg, attempts: int = 3) -> None:
    for i in range(attempts):
        try:
            _HANDLERS[kind](arg)
            _STATE["synced"] += 1
            _STATE["last_ok"] = db.now()
            return
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker, name="gsync", daemon=True)
            _WORKER.start()


def _enqueue(kind: str, arg) -> None:
    if not enabled():
        return
    _ensure_worker()
    _QUEUE.put((kind, arg))


def queue_application(code: str) -> None:
    _enqueue("application", code)


def queue_consultation(cid: int) -> None:
    _enqueue("consultation", cid)


def queue_document(doc_id: int) -> None:
    _enqueue("document", doc_id)


def pending() -> int:
    """대기 중 + 처리 중인 작업 수. 큐에서 꺼내 처리하는 동안도 아직 안 끝난 것으로 센다."""
    return _QUEUE.unfinished_tasks


def flush(timeout: float = 30.0) -> bool:
    """진행 중인 동기화가 모두 끝날 때까지 기다린다 (테스트·전체 동기화 확인용)."""
    deadline = time.time() + timeout
    while pending() and time.time() < deadline:
        time.sleep(0.02)
    return pending() == 0


def sync_all() -> dict:
    """DB 전체를 시트로 다시 밀어 넣는다 (관리자 화면의 '전체 동기화')."""
    if not enabled():
        raise RuntimeError("구글 시트 연동이 설정되지 않았습니다.")
    with db.db() as conn:
        codes = [r["code"] for r in conn.execute("SELECT code FROM applications ORDER BY id")]
        cids = [r["id"] for r in conn.execute("SELECT id FROM consultations ORDER BY id")]
        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE drive_url='' ORDER BY id")]
    for code in codes:
        queue_application(code)
    for cid in cids:
        queue_consultation(cid)
    for doc_id in doc_ids:
        queue_document(doc_id)
    return {"applications": len(codes), "consultations": len(cids), "documents": len(doc_ids)}


def status() -> dict:
    return {
        "enabled": enabled(),
        "sheet_url": sheet_url(),
        "drive_enabled": bool(folder_id()),
        "pending": pending(),
        **_STATE,
    }


def reset_for_test() -> None:
    """테스트에서 자격증명 캐시와 통계를 비운다."""
    global _CREDS
    _CREDS = None
    _STATE.update({"last_ok": "", "last_error": "", "synced": 0, "failed": 0})
