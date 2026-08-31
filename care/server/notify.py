"""카카오 알림톡 자동 발송.

진행 단계가 바뀔 때마다 신청자에게 알림톡을 보낸다. 이 서비스의 모든 안내는
알림톡으로만 나가므로, 신청자는 다이음 카카오채널 친구추가를 유지해야 한다.

발송 경로 (환경변수로 선택):
    ALIMTALK_MODE=dryrun   기본값. 실제로 보내지 않고 "무엇이 나갔을지"만 기록한다.
                           업체 계약 전이나 개발 중에 문구를 확인하는 용도.
    ALIMTALK_MODE=webhook  ALIMTALK_WEBHOOK_URL 로 발송 요청을 POST 한다.
                           알림톡 업체(솔라피·알리고·NHN 등) 어느 쪽을 쓰든
                           그 앞단에 얇은 중계만 두면 되도록 만든 통로다.

    새 업체를 직접 붙이려면 _SENDERS 에 함수 하나만 추가하면 된다.

중요 — 코드만으로는 발송이 되지 않는다:
    알림톡은 카카오 비즈메시지 발신프로필 등록과 **템플릿 사전 심사 승인**이 필요하다.
    아래 TEMPLATES 의 body 가 그 심사에 올릴 문구이고, 승인된 템플릿 ID를
    ALIMTALK_TEMPLATE_IDS(JSON)에 넣어야 실제 발송이 된다.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from datetime import datetime

from . import db

# 변수는 {중괄호}로 표기한다 — 카카오 알림톡 템플릿의 #{변수} 자리에 대응한다.
TEMPLATES: dict[str, dict] = {
    "received": {
        "title": "접수가 완료되었습니다",
        "when": "신청서를 제출해 접수가 자동 승인된 직후",
        "body": (
            "[다이음 다이렉트] 접수 완료\n\n"
            "{mother_name} 산모님, 신청이 접수되었습니다.\n"
            "접수번호: {code}\n"
            "산후도우미: {helper_name}\n\n"
            "아직 올리지 않은 서류: {missing}\n\n"
            "이제부터 진행 상황은 카카오톡에서 바로 확인하실 수 있습니다.\n"
            "아래 버튼을 누르면 서류가 어디까지 검토됐는지, 무엇이 더 필요한지 언제든 보실 수 있어요.\n\n"
            "채널을 그대로 두시면 매달 바뀌는 친정엄마 급여와 새 이벤트 소식도 먼저 받아보실 수 있습니다."
        ),
        "button": "진행상황 확인하기",
    },
    "reviewing": {
        "title": "서류 검토를 시작했습니다",
        "when": "관리자가 검토중으로 변경했을 때",
        "body": (
            "[다이음 다이렉트] 서류 검토중\n\n"
            "{mother_name} 산모님, 제출하신 서류를 확인하고 있습니다.\n"
            "접수번호: {code}\n\n"
            "결과가 나오면 이 채널로 알려드립니다. 지금 상태는 아래 버튼에서 확인하실 수 있어요."
        ),
        "button": "진행상황 확인하기",
    },
    "rejected": {
        "title": "보완이 필요합니다",
        "when": "관리자가 부족 서류를 지정해 반려했을 때",
        "body": (
            "[다이음 다이렉트] 서류 보완 요청\n\n"
            "{mother_name} 산모님, 아래 서류가 더 필요합니다.\n"
            "접수번호: {code}\n"
            "부족한 서류: {missing}\n\n"
            "안내: {message}\n\n"
            "아래 버튼에서 부족한 서류만 추가로 올리신 뒤 다시 제출해 주세요. "
            "처음부터 다시 작성하실 필요는 없습니다."
        ),
        "button": "부족한 서류 올리기",
    },
    "confirmed": {
        "title": "최종확인이 완료되었습니다",
        "when": "관리자가 최종확인 완료로 변경했을 때",
        "body": (
            "[다이음 다이렉트] 최종확인 완료\n\n"
            "{mother_name} 산모님, 모든 서류가 확인되었습니다.\n"
            "접수번호: {code}\n\n"
            "이후 근로계약 체결과 제공기록지 작성은 기존 다이음 시스템에서 그대로 진행됩니다. "
            "담당자가 일정 안내를 위해 연락드립니다.\n\n"
            "채널은 그대로 두시면 다음 이용 안내와 이벤트 소식을 계속 받아보실 수 있습니다."
        ),
        "button": "접수 내용 다시 보기",
    },
    "doc_request": {
        "title": "서류를 올려주세요",
        "when": "관리자가 필요한 서류를 카카오톡으로 요청했을 때 (접수 직후 첫 안내, 이후 보완 요청)",
        "body": (
            "[다이음 다이렉트] 서류 제출 안내\n\n"
            "{mother_name} 산모님, 아래 서류를 올려주세요.\n"
            "접수번호: {code}\n"
            "필요한 서류: {missing}\n\n"
            "{message}\n\n"
            "아래 버튼을 누르면 서류를 올리는 화면이 바로 열립니다. "
            "준비되는 대로 하나씩 올리셔도 됩니다."
        ),
        "button": "서류 올리기",
    },
    "consult": {
        "title": "전화상담 신청이 접수되었습니다",
        "when": "전화상담을 신청했을 때",
        "body": (
            "[다이음 다이렉트] 전화상담 접수\n\n"
            "{name}님, 상담 신청이 접수되었습니다.\n"
            "희망 시간: {preferred_time}\n\n"
            "담당자가 순차적으로 연락드립니다."
        ),
    },
}

# 상태가 이 값으로 바뀌면 해당 템플릿을 보낸다.
STATUS_TEMPLATE = {
    "received": "received",
    "reviewing": "reviewing",
    "rejected": "rejected",
    "confirmed": "confirmed",
}

_QUEUE: "queue.Queue[int]" = queue.Queue()
_WORKER: threading.Thread | None = None
_LOCK = threading.Lock()
_STATE = {"sent": 0, "failed": 0, "skipped": 0, "last_error": "", "last_sent": ""}


# ── 설정 ────────────────────────────────────────────────────

def mode() -> str:
    return (os.environ.get("ALIMTALK_MODE") or "dryrun").strip().lower()


def webhook_url() -> str:
    return (os.environ.get("ALIMTALK_WEBHOOK_URL") or "").strip()


def public_base() -> str:
    """알림톡 버튼이 열 주소의 기준. 예: https://care.dayeum.co.kr"""
    return (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def status_link(code: str, token: str) -> str:
    base = public_base()
    if not (base and code and token):
        return ""
    return f"{base}/status.html?code={code}&token={token}"


def template_ids() -> dict:
    raw = (os.environ.get("ALIMTALK_TEMPLATE_IDS") or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def live() -> bool:
    """실제로 발송되는 상태인지."""
    return mode() == "webhook" and bool(webhook_url())


# ── 문구 만들기 ──────────────────────────────────────────────

def render(template_key: str, values: dict) -> tuple[str, str]:
    """템플릿에 값을 채워 (제목, 본문)을 만든다. 빈 값은 '-' 로 채운다."""
    tpl = TEMPLATES.get(template_key)
    if not tpl:
        raise KeyError(f"알 수 없는 알림톡 템플릿: {template_key}")
    safe = {k: (str(v).strip() or "-") for k, v in values.items()}
    return tpl["title"], tpl["body"].format_map(_Blank(safe))


class _Blank(dict):
    def __missing__(self, key):  # 템플릿에 값이 안 넘어와도 발송이 깨지지 않게
        return "-"


def _application_values(app: dict, missing_labels: list[str]) -> dict:
    return {
        "link": status_link(app.get("code", ""), app.get("token", "")),
        "code": app.get("code", ""),
        "mother_name": app.get("mother_name", ""),
        "helper_name": app.get("helper_name", ""),
        "message": app.get("admin_message", ""),
        "missing": ", ".join(missing_labels) if missing_labels else "없음",
    }


# ── 발송 ────────────────────────────────────────────────────

def _send_dryrun(row: dict) -> tuple[str, str]:
    return "skipped", "발송하지 않음 (ALIMTALK_MODE=dryrun — 문구 확인용)"


def _send_webhook(row: dict) -> tuple[str, str]:
    import requests

    url = webhook_url()
    if not url:
        return "skipped", "ALIMTALK_WEBHOOK_URL 이 설정되지 않았습니다."
    payload = {
        "template_key": row["template_key"],
        "template_id": template_ids().get(row["template_key"], ""),
        "to": row["phone"],
        "title": row["title"],
        "text": row["body"],
    }
    button = TEMPLATES.get(row["template_key"], {}).get("button")
    if button and row["link"]:
        # 알림톡 웹링크 버튼 — 카카오톡에서 바로 조회 화면이 열린다
        payload["button"] = {"name": button, "type": "WL", "url": row["link"]}
    headers = {"Content-Type": "application/json"}
    secret = (os.environ.get("ALIMTALK_WEBHOOK_SECRET") or "").strip()
    if secret:
        headers["X-Webhook-Secret"] = secret
    res = requests.post(url, json=payload, headers=headers, timeout=20)
    if res.status_code >= 400:
        raise RuntimeError(f"발송 실패 {res.status_code}: {res.text[:200]}")
    return "sent", f"HTTP {res.status_code}"


_SENDERS = {"dryrun": _send_dryrun, "webhook": _send_webhook}


def _deliver(notification_id: int) -> None:
    with db.db() as conn:
        row = conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        if row is None or row["status"] not in ("queued", "failed"):
            return
        data = dict(row)
    sender = _SENDERS.get(mode(), _send_dryrun)
    try:
        status, detail = sender(data)
    except Exception as exc:
        status, detail = "failed", str(exc)[:300]
        _STATE["failed"] += 1
        _STATE["last_error"] = f"[{db.now()}] {detail}"
    else:
        if status == "sent":
            _STATE["sent"] += 1
            _STATE["last_sent"] = db.now()
        else:
            _STATE["skipped"] += 1
    with db.db() as conn:
        conn.execute(
            "UPDATE notifications SET status=?, detail=?, sent_at=? WHERE id=?",
            (status, detail, db.now() if status == "sent" else "", notification_id),
        )


def _worker() -> None:
    while True:
        # get 과 task_done 은 반드시 같은 큐 객체여야 한다. 전역을 두 번 읽으면
        # 그 사이에 큐가 교체됐을 때 짝이 어긋난다.
        q = _QUEUE
        nid = q.get()
        try:
            _deliver(nid)
        except Exception:
            _STATE["last_error"] = traceback.format_exc(limit=2)[-300:]
            print(f"[notify] 발송 처리 실패: {_STATE['last_error']}", flush=True)
        finally:
            q.task_done()


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker, name="notify", daemon=True)
            _WORKER.start()


def _record(conn, app_id: int | None, template_key: str, phone: str, values: dict) -> int:
    title, body = render(template_key, values)
    cur = conn.execute(
        "INSERT INTO notifications(application_id, template_key, phone, title, body, link, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (app_id, template_key, phone, title, body, values.get("link", ""), db.now()),
    )
    return int(cur.lastrowid)


def queue_status_change(conn, app: dict, missing_labels: list[str]) -> int | None:
    """상태 변경에 해당하는 알림톡을 예약한다. 대상 템플릿이 없으면 아무것도 안 한다."""
    key = STATUS_TEMPLATE.get(app.get("status", ""))
    if not key:
        return None
    nid = _record(conn, app.get("id"), key, app.get("mother_phone", ""),
                  _application_values(app, missing_labels))
    return nid


def queue_doc_request(conn, app: dict, labels: list[str], message: str) -> int:
    return _record(conn, app.get("id"), "doc_request", app.get("mother_phone", ""), {
        "link": status_link(app.get("code", ""), app.get("token", "")),
        "code": app.get("code", ""), "mother_name": app.get("mother_name", ""),
        "missing": ", ".join(labels), "message": message,
    })


def queue_consultation(conn, c: dict) -> int:
    return _record(conn, None, "consult", c.get("phone", ""), {
        "name": c.get("name", ""), "preferred_time": c.get("preferred_time", ""),
    })


def dispatch(notification_id: int | None) -> None:
    """예약된 알림을 백그라운드에서 실제 발송한다 (DB 커밋 이후에 호출)."""
    if notification_id is None:
        return
    _ensure_worker()
    _QUEUE.put(notification_id)


def resend(notification_id: int) -> None:
    with db.db() as conn:
        conn.execute("UPDATE notifications SET status='queued', detail='' WHERE id=?", (notification_id,))
    dispatch(notification_id)


def flush(timeout: float = 20.0) -> bool:
    import time
    deadline = time.time() + timeout
    while _QUEUE.unfinished_tasks and time.time() < deadline:
        time.sleep(0.02)
    return _QUEUE.unfinished_tasks == 0


def status() -> dict:
    return {
        "mode": mode(),
        "live": live(),
        "webhook_set": bool(webhook_url()),
        "template_ids": sorted(template_ids().keys()),
        "pending": _QUEUE.unfinished_tasks,
        "public_base": public_base(),
        "templates": [
            {"key": k, "title": v["title"], "when": v["when"], "body": v["body"],
             "button": v.get("button", "")}
            for k, v in TEMPLATES.items()
        ],
        **_STATE,
    }


def reset_for_test() -> None:
    _STATE.update({"sent": 0, "failed": 0, "skipped": 0, "last_error": "", "last_sent": ""})
