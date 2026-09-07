"""설문폼 → 카카오 알림톡 자동 발송 웹 서비스.

실행 (kakao-alimtalk/ 에서):
    uvicorn server.app:app --host 0.0.0.0 --port 8000

환경변수:
    WEBHOOK_SECRET   설문폼 웹훅 인증용 비밀값 (필수) — ?secret= 또는 X-Webhook-Secret 헤더
    ADMIN_PASSWORD   대시보드/관리 API 비밀번호 (필수)
    ALIMTALK_PROVIDER  console | solapi | aligo   (rules.yaml 의 provider 를 덮어씀)
    ALIMTALK_SENDER    발신번호
    SOLAPI_API_KEY / SOLAPI_API_SECRET / SOLAPI_PF_ID
    ALIGO_API_KEY / ALIGO_USER_ID / ALIGO_SENDER_KEY
    ALIMTALK_DRY_RUN=1 실발송 없이 로그만
    RULES_PATH, DB_PATH, WORKER_ENABLED(기본 1), WORKER_INTERVAL(초, 기본 15)
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alimtalk.config import BASE_DIR, Config, load_config  # noqa: E402
from alimtalk.models import Job  # noqa: E402
from alimtalk.phone import format_phone, mask_phone, normalize_phone  # noqa: E402
from alimtalk.providers import ProviderError, get_provider  # noqa: E402
from alimtalk.scheduler import Worker  # noqa: E402
from alimtalk.service import deliver, intake  # noqa: E402
from alimtalk.store import Store  # noqa: E402
from alimtalk.templates import builtin_vars, render, render_variables  # noqa: E402

from .webhooks import normalize_payload  # noqa: E402

load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("alimtalk.server")

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


class State:
    cfg: Config
    store: Store
    worker: Worker | None = None
    provider_error: str | None = None


state = State()


def _make_provider():
    try:
        return get_provider(state.cfg.provider)
    except ProviderError as e:
        state.provider_error = str(e)
        log.error("발송사 설정 오류 — 콘솔(dry-run) 모드로 대체합니다: %s", e)
        from alimtalk.providers import ConsoleProvider
        return ConsoleProvider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.cfg = load_config()
    state.store = Store()
    state.provider_error = None
    provider = _make_provider()
    if os.environ.get("WORKER_ENABLED", "1") not in ("0", "false", "no"):
        state.worker = Worker(state.cfg, state.store, provider, interval=float(os.environ.get("WORKER_INTERVAL", "15")))
        state.worker.start()
    log.info("시작: provider=%s forms=%s", provider.name, [f.id for f in state.cfg.forms])
    yield
    if state.worker:
        state.worker.stop()
    state.store.close()


app = FastAPI(title="카카오 알림톡 자동 발송", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── 인증 ───────────────────────────────────────────────
def _check_secret(secret_q: str | None, secret_h: str | None) -> None:
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not expected:
        raise HTTPException(503, "서버에 WEBHOOK_SECRET 이 설정되지 않아 웹훅이 비활성화되어 있습니다.")
    given = secret_h or secret_q or ""
    if not hmac.compare_digest(given, expected):
        raise HTTPException(401, "웹훅 비밀값이 올바르지 않습니다.")


def _check_admin(x_admin_password: str | None) -> None:
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(403, "서버에 ADMIN_PASSWORD 가 설정되지 않아 관리 기능이 비활성화되어 있습니다.")
    if not x_admin_password or not hmac.compare_digest(x_admin_password, expected):
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")


def _job_dict(j: Job) -> dict[str, Any]:
    tz = ZoneInfo(state.cfg.timezone)
    f = lambda d: d.astimezone(tz).strftime("%Y-%m-%d %H:%M") if d else None  # noqa: E731
    return {
        "id": j.id, "form_id": j.form_id, "message": j.message_name, "phone": mask_phone(j.phone),
        "template": j.template_code, "variables": j.variables, "text": j.text,
        "scheduled_at": f(j.scheduled_at), "status": j.status, "attempts": j.attempts,
        "sent_at": f(j.sent_at), "created_at": f(j.created_at),
        "error": (j.result or {}).get("error"), "message_id": (j.result or {}).get("message_id"),
        "channel": (j.result or {}).get("channel"),
    }


# ── 공개 ───────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(DASHBOARD, media_type="text/html")


@app.get("/healthz")
@app.get("/api/health")
def health():
    return {"ok": True, "provider": state.cfg.provider, "dry_run": os.environ.get("ALIMTALK_DRY_RUN", "") in ("1", "true"),
            "provider_error": state.provider_error, "forms": [f.id for f in state.cfg.forms],
            "webhook_configured": bool(os.environ.get("WEBHOOK_SECRET"))}


# ── 웹훅 (설문폼 → 서버) ─────────────────────────────────
@app.post("/webhook/form")
@app.post("/webhook/form/{form_id}")
async def webhook_form(request: Request, form_id: str | None = None,
                       form: str | None = Query(None), secret: str | None = Query(None),
                       x_webhook_secret: str | None = Header(None)):
    """구글폼(Apps Script)·Tally·Typeform·Zapier 등에서 호출. 전화번호 항목을 찾아 발송을 예약한다."""
    _check_secret(secret, x_webhook_secret)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        form_data = await request.form()
        body = dict(form_data)
    try:
        answers, meta = normalize_payload(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    fid = form_id or form or (meta.get("form") if state.cfg.form(str(meta.get("form") or "")) else None) or "default"
    r = intake(state.cfg, state.store, fid, answers, source=f"webhook:{fid}",
               phone_hint=meta.get("phone"), name_hint=meta.get("name"))
    # 예약이 즉시 발송이면 워커를 기다리지 않고 바로 처리
    if r.accepted and state.worker:
        state.worker.tick()
    return {"ok": r.accepted, **r.to_dict(), "phone": mask_phone(r.phone) if r.phone else None}


# ── 관리 API ───────────────────────────────────────────
@app.get("/api/overview")
def overview(x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    cfg = state.cfg
    return {
        "counts": state.store.counts(),
        "provider": cfg.provider, "provider_error": state.provider_error,
        "dry_run": os.environ.get("ALIMTALK_DRY_RUN", "") in ("1", "true"),
        "sender": cfg.sender, "timezone": cfg.timezone, "quiet_hours": cfg.quiet_hours,
        "forms": [{"id": f.id, "require_consent": f.require_consent, "dedupe_hours": f.dedupe_hours,
                   "messages": [{"name": m.name, "template": m.template, "when": m.when,
                                 "variables": m.variables, "text": m.text} for m in f.messages]}
                  for f in cfg.forms],
        "webhook_url_hint": "/webhook/form?form=<폼id>&secret=<WEBHOOK_SECRET>",
    }


@app.get("/api/jobs")
def list_jobs(status: str | None = None, limit: int = 200, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    return {"jobs": [_job_dict(j) for j in state.store.list_jobs(status, min(limit, 1000))]}


@app.get("/api/submissions")
def list_submissions(limit: int = 100, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    out = []
    for s in state.store.list_submissions(min(limit, 500)):
        s["phone"] = mask_phone(s["phone"])
        out.append(s)
    return {"submissions": out}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    j = state.store.get_job(job_id)
    if not j:
        raise HTTPException(404, "작업이 없습니다")
    if j.status != "pending":
        raise HTTPException(409, f"대기 중 작업만 취소할 수 있습니다 (현재: {j.status})")
    j.status = "cancelled"
    state.store.update_job(j)
    return {"ok": True, "job": _job_dict(j)}


@app.post("/api/jobs/{job_id}/send-now")
def send_now(job_id: int, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    j = state.store.get_job(job_id)
    if not j:
        raise HTTPException(404, "작업이 없습니다")
    if j.status not in ("pending", "failed"):
        raise HTTPException(409, f"대기/실패 작업만 즉시 발송할 수 있습니다 (현재: {j.status})")
    j.status, j.attempts = "pending", 0
    res = deliver(state.cfg, state.store, _make_provider(), j)
    return {"ok": res.ok, "result": res.to_dict(), "job": _job_dict(j)}


class TestSend(BaseModel):
    phone: str
    form: str = "default"
    message: str | None = None
    template: str | None = None
    text: str | None = None
    variables: dict[str, str] = {}


@app.post("/api/send-test")
def send_test(body: TestSend, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "휴대폰 번호 형식이 아닙니다")
    cfg = state.cfg
    form = cfg.form(body.form)
    rule = next((m for m in (form.messages if form else []) if m.name == body.message), None)
    if rule is None and not body.template:
        raise HTTPException(400, "메시지 이름(rules.yaml) 또는 템플릿 코드를 지정하세요")
    now = datetime.now(ZoneInfo(cfg.timezone))
    builtins = builtin_vars(body.variables.get("이름", body.variables.get("name", "")), phone, now)
    job = Job(id=None, submission_id=None, form_id=form.id if form else "manual",
              message_name=body.message or "테스트", phone=phone,
              template_code=body.template or rule.template,
              variables=render_variables(rule.variables, body.variables, builtins) if rule else body.variables,
              text=render(rule.text, body.variables, builtins) if rule else body.text,
              subject=render(rule.subject, body.variables, builtins) if rule else None,
              scheduled_at=now)
    state.store.add_job(job)
    res = deliver(cfg, state.store, _make_provider(), job)
    return {"ok": res.ok, "result": res.to_dict(), "job": _job_dict(job)}


class BulkIntake(BaseModel):
    form: str = "default"
    rows: list[dict[str, Any]]


@app.post("/api/bulk")
def bulk(body: BulkIntake, x_admin_password: str | None = Header(None)):
    """구글시트/엑셀에서 복사한 명단(헤더 포함)을 한 번에 접수한다."""
    _check_admin(x_admin_password)
    if len(body.rows) > 2000:
        raise HTTPException(400, "한 번에 2000행까지 처리할 수 있습니다")
    results = []
    for i, row in enumerate(body.rows, 1):
        r = intake(state.cfg, state.store, body.form, row, source="bulk")
        results.append({"row": i, "accepted": r.accepted, "reason": r.reason,
                        "phone": mask_phone(r.phone) if r.phone else None, "jobs": len(r.jobs)})
    accepted = sum(1 for r in results if r["accepted"])
    if accepted and state.worker:
        state.worker.tick()
    return {"total": len(results), "accepted": accepted, "results": results}


class OptOut(BaseModel):
    phone: str
    reason: str = ""


@app.get("/api/optouts")
def list_optouts(x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    return {"optouts": [{**o, "phone": format_phone(o["phone"])} for o in state.store.list_optouts()]}


@app.post("/api/optouts")
def add_optout(body: OptOut, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "휴대폰 번호 형식이 아닙니다")
    state.store.add_optout(phone, body.reason)
    return {"ok": True}


@app.delete("/api/optouts/{phone}")
def remove_optout(phone: str, x_admin_password: str | None = Header(None)):
    _check_admin(x_admin_password)
    p = normalize_phone(phone)
    if not p:
        raise HTTPException(400, "휴대폰 번호 형식이 아닙니다")
    state.store.remove_optout(p)
    return {"ok": True}


@app.post("/api/reload")
def reload_config(x_admin_password: str | None = Header(None)):
    """rules.yaml 을 다시 읽는다 (서버 재시작 없이 규칙 수정 반영)."""
    _check_admin(x_admin_password)
    state.cfg = load_config()
    if state.worker:
        state.worker.cfg = state.cfg
        state.worker.provider = _make_provider()
    return {"ok": True, "forms": [f.id for f in state.cfg.forms]}


@app.exception_handler(ProviderError)
async def _provider_error(request, exc: ProviderError):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
