"""제휴 가게 연동 · 친정엄마 산후도우미 접수/양방향 인증 API.

실행 (partner-care/ 에서):
    uvicorn app.main:app --host 0.0.0.0 --port 8100

환경변수
    SERVER_SECRET              서명 키 (운영 필수)
    DAIEUM_ADMIN_PASSWORD      다이음 관리자 비밀번호
    PARTNER_ADMIN_PASSWORD__<파트너ID>  가게 관리자 비밀번호 (설정 파일보다 우선)
    PARTNER_CONFIG             파트너 카탈로그 JSON 경로 (기본 config/partners.json)
    DATA_DIR                   접수 데이터 저장 경로 (기본 partner-care/data)
    OTP_DEBUG=1                문자 발송 대신 인증번호를 응답/로그에 노출(개발용)
"""

from __future__ import annotations

import hmac
import os
import re
import time

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, security, store
from .config import BASE_DIR, ConfigError
from .models import (
    APPROVED,
    CANCELLED,
    EXPIRED,
    FULFILLED,
    OPERATOR,
    PENDING,
    PHONE_PENDING,
    REJECTED,
    STORE,
    Claim,
    new_id,
    now,
)

WEB_DIR = BASE_DIR / "web"

OTP_TTL = 5 * 60          # 인증번호 유효시간
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = 30
CLAIM_TOKEN_TTL = 60 * 60 * 24 * 30   # 고객이 접수 상태를 다시 조회할 수 있는 기간
ADMIN_TOKEN_TTL = 60 * 60 * 12

app = FastAPI(title="다이음 제휴 접수·양방향 인증", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.exception_handler(ConfigError)
async def _config_error(request, exc: ConfigError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=500, content={"detail": f"설정 오류: {exc}"})


# ── 공통 헬퍼 ─────────────────────────────────────────────
def _partner_or_404(partner_id: str) -> dict:
    p = config.partner(partner_id)
    if not p:
        raise HTTPException(404, f"등록되지 않은 제휴처입니다: {partner_id}")
    return p


def _claim_or_404(claim_id: str) -> Claim:
    c = store.get(claim_id)
    if not c:
        raise HTTPException(404, f"접수 건을 찾을 수 없습니다: {claim_id}")
    return c


def _refresh(claim: Claim) -> Claim:
    """상태를 재계산해 저장(승인 무효화·만료 반영).

    제휴처가 설정에서 빠져도 기존 접수 건은 조회할 수 있어야 하므로 기본 규칙으로 대체한다.
    """
    partner = config.partner(claim.partner_id)
    rules = partner["rules"] if partner else dict(config.DEFAULT_RULES)
    before = claim.status
    claim.refresh_status(rules)
    if claim.status != before:
        claim.updated_at = now()
        store.save(claim)
        store.audit(claim.id, "status_changed", "system",
                    {"from": before, "to": claim.status})
    return claim


def _applicant(claim_id: str, token: str | None) -> Claim:
    data = security.read_token(token)
    if not data or data.get("t") != "claim" or data.get("cid") != claim_id:
        raise HTTPException(401, "본인확인이 필요합니다. 접수 시 인증한 휴대폰으로 다시 인증해 주세요.")
    claim = _claim_or_404(claim_id)
    if data.get("pk") != claim.phone_key:
        # 접수자의 휴대폰이 바뀌었다면 그 토큰은 더 이상 본인의 것이 아니다.
        raise HTTPException(401, "본인확인 정보가 변경되어 다시 인증이 필요합니다.")
    return claim


def _admin(token: str | None) -> dict:
    data = security.read_token(token)
    if not data or data.get("t") != "admin":
        raise HTTPException(401, "관리자 로그인이 필요합니다.")
    return data


def _admin_can_see(admin: dict, claim: Claim) -> bool:
    return admin["role"] == OPERATOR or admin.get("pid") == claim.partner_id


def _validate_fields(spec: list[dict], values: dict, what: str) -> dict:
    """설정 파일에 정의된 항목 규격대로 입력값 검증 — 가게마다 항목이 다르므로."""
    cleaned: dict = {}
    for f in spec:
        key, label = f["key"], f.get("label", f["key"])
        raw = values.get(key)
        val = ("" if raw is None else str(raw)).strip()
        if not val:
            if f.get("required"):
                raise HTTPException(400, f"{what}: '{label}' 을(를) 입력해 주세요.")
            continue
        if len(val) > 500:
            raise HTTPException(400, f"{what}: '{label}' 이(가) 너무 깁니다.")
        pattern = f.get("pattern")
        if pattern and not re.fullmatch(pattern, val):
            raise HTTPException(400, f"{what}: '{label}' 형식이 올바르지 않습니다. {f.get('hint', '')}".strip())
        options = f.get("options")
        if options and val not in options:
            raise HTTPException(400, f"{what}: '{label}' 은(는) {', '.join(options)} 중에서 선택해 주세요.")
        cleaned[key] = val
    return cleaned


def _field_labels(partner: dict, service: dict | None) -> dict:
    """설정에 정의된 항목 key → 표시 이름. 관리자 화면이 raw key 대신 라벨을 쓰게 한다."""
    labels = {f["key"]: f.get("label", f["key"]) for f in partner.get("identity_fields", [])}
    for f in (service or {}).get("booking_fields", []):
        labels[f["key"]] = f.get("label", f["key"])
    return labels


def _send_otp(claim: Claim) -> str | None:
    """인증번호 발급. 실제 SMS 연동 지점(현재는 로그 출력/개발 모드 노출)."""
    code = security.new_otp()
    claim.otp = {
        "digest": security.otp_digest(claim.id, code),
        "expires_at": now() + OTP_TTL,
        "attempts": 0,
        "sent_at": now(),
    }
    store.save(claim)
    print(f"[OTP] {claim.id} {claim.phone_masked} → {code}", flush=True)
    return code if os.environ.get("OTP_DEBUG") == "1" else None


# ── 정적 페이지 ───────────────────────────────────────────
def _page(name: str) -> FileResponse:
    path = WEB_DIR / name
    if not path.exists():
        raise HTTPException(404, "페이지를 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/html")


@app.get("/")
def index():
    return _page("index.html")


@app.get("/apply")
def apply_page():
    return _page("apply.html")


@app.get("/store")
def store_page():
    return _page("store-admin.html")


@app.get("/daieum")
def daieum_page():
    return _page("daieum-admin.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response

    return Response(status_code=204)


@app.get("/api/health")
def health():
    cfg = config.load()
    return {
        "ok": True,
        "operator": config.operator()["name"],
        "partners": len(cfg["partners"]),
        "services": list(cfg["services"].keys()),
    }


# ── 카탈로그(공개) ────────────────────────────────────────
@app.get("/api/catalog")
def catalog():
    cfg = config.load()
    return {
        "operator": config.operator(),
        "services": cfg["services"],
        "partners": [config.public_partner(p) for p in config.partners()],
    }


@app.get("/api/partners/{partner_id}")
def get_partner(partner_id: str):
    p = _partner_or_404(partner_id)
    if not p.get("active"):
        raise HTTPException(410, f"{p['name']} 제휴는 현재 접수를 받지 않습니다.")
    cfg = config.load()
    return {
        "operator": config.operator(),
        "partner": config.public_partner(p),
        "services": {sid: cfg["services"][sid] for sid in p["services"]},
    }


# ── 고객: 접수 ────────────────────────────────────────────
class ClaimCreate(BaseModel):
    partner_id: str
    service_id: str
    product_id: str
    name: str = Field(min_length=1, max_length=40)
    phone: str
    identity: dict = {}
    booking: dict = {}
    agree_terms: bool = False
    agree_privacy: bool = False
    agree_no_transfer: bool = False


@app.post("/api/claims", status_code=201)
def create_claim(body: ClaimCreate):
    partner = _partner_or_404(body.partner_id)
    if not partner.get("active"):
        raise HTTPException(410, f"{partner['name']} 제휴는 현재 접수를 받지 않습니다.")
    if body.service_id not in partner["services"]:
        raise HTTPException(400, "이 제휴처에서 제공하지 않는 서비스입니다.")
    service = config.service(body.service_id)
    product = config.product(body.partner_id, body.product_id)
    if not product:
        raise HTTPException(400, "선택한 제휴 상품이 없습니다.")
    if not (body.agree_terms and body.agree_privacy):
        raise HTTPException(400, "이용약관과 개인정보 수집·이용에 동의해야 접수할 수 있습니다.")
    if not partner["rules"]["transfer_allowed"] and not body.agree_no_transfer:
        raise HTTPException(400, "본인 외 타인에게 양도하지 않는다는 확인에 동의해야 합니다.")
    if not security.valid_phone(body.phone):
        raise HTTPException(400, "휴대폰 번호 형식이 올바르지 않습니다.")

    identity = _validate_fields(partner["identity_fields"], body.identity, "본인확인 정보")
    booking = _validate_fields(service.get("booking_fields", []), body.booking, "예약 정보")

    pkey = security.phone_key(body.phone)
    limit = partner["rules"]["max_claims_per_person"]
    if limit and store.count_for_person(body.partner_id, pkey) >= limit:
        raise HTTPException(
            409,
            f"이미 접수된 건이 있습니다. {partner['name']} 제휴 혜택은 1인 {limit}회까지 신청할 수 있습니다.",
        )

    digits = security.normalize_phone(body.phone)
    claim = Claim(
        id=new_id(),
        partner_id=body.partner_id,
        service_id=body.service_id,
        product_id=body.product_id,
        status=PHONE_PENDING,
        applicant_name=body.name.strip(),
        phone_masked=security.mask_phone(body.phone),
        phone_key=pkey,
        phone_last4=digits[-4:],
        identity=identity,
        booking=booking,
        consent={
            "terms": body.agree_terms,
            "privacy": body.agree_privacy,
            "no_transfer": body.agree_no_transfer or partner["rules"]["transfer_allowed"],
            "at": now(),
        },
        created_at=now(),
        updated_at=now(),
        expires_at=now() + partner["rules"]["claim_valid_days"] * 86400,
    )
    store.save(claim)
    store.audit(claim.id, "created", "applicant",
                {"partner": partner["id"], "product": product["id"],
                 "fingerprint": claim.fingerprint()[:16]})
    dev_code = _send_otp(claim)
    return {
        "claim_id": claim.id,
        "status": claim.status,
        "message": f"{claim.phone_masked} 로 인증번호를 보냈습니다. 5분 안에 입력해 주세요.",
        **({"dev_code": dev_code} if dev_code else {}),
    }


@app.post("/api/claims/{claim_id}/otp/resend")
def resend_otp(claim_id: str):
    claim = _claim_or_404(claim_id)
    if claim.status != PHONE_PENDING:
        raise HTTPException(400, "이미 본인확인이 완료된 접수입니다.")
    if now() - claim.otp.get("sent_at", 0) < OTP_RESEND_COOLDOWN:
        raise HTTPException(429, f"{OTP_RESEND_COOLDOWN}초 후에 다시 요청해 주세요.")
    dev_code = _send_otp(claim)
    store.audit(claim.id, "otp_resent", "applicant", {})
    return {"message": "인증번호를 다시 보냈습니다.", **({"dev_code": dev_code} if dev_code else {})}


class OtpVerify(BaseModel):
    code: str


@app.post("/api/claims/{claim_id}/otp/verify")
def verify_otp(claim_id: str, body: OtpVerify):
    claim = _claim_or_404(claim_id)
    if claim.status != PHONE_PENDING:
        raise HTTPException(400, "이미 본인확인이 완료된 접수입니다.")
    otp = claim.otp or {}
    if not otp or otp.get("expires_at", 0) < now():
        raise HTTPException(400, "인증번호가 만료되었습니다. 다시 요청해 주세요.")
    if otp.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise HTTPException(429, "인증 시도 횟수를 초과했습니다. 인증번호를 다시 요청해 주세요.")
    otp["attempts"] = otp.get("attempts", 0) + 1
    claim.otp = otp
    store.save(claim)
    if not hmac.compare_digest(
        otp.get("digest", ""), security.otp_digest(claim.id, re.sub(r"\D", "", body.code or ""))
    ):
        store.audit(claim.id, "otp_failed", "applicant", {"attempts": otp["attempts"]})
        raise HTTPException(400, "인증번호가 올바르지 않습니다.")

    claim.otp = {}
    claim.status = PENDING
    claim.updated_at = now()
    store.save(claim)
    store.audit(claim.id, "phone_verified", "applicant", {"phone": claim.phone_masked})
    rules = _partner_or_404(claim.partner_id)["rules"]
    return {
        "token": security.issue_token(
            {"t": "claim", "cid": claim.id, "pk": claim.phone_key}, CLAIM_TOKEN_TTL
        ),
        "claim": claim.public_view(rules),
        "message": "본인확인이 완료되었습니다. 제휴처와 다이음의 확인을 기다려 주세요.",
    }


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str, x_claim_token: str | None = Header(default=None)):
    claim = _refresh(_applicant(claim_id, x_claim_token))
    partner = _partner_or_404(claim.partner_id)
    view = claim.public_view(partner["rules"])
    view["partner_name"] = partner["name"]
    view["service_name"] = (config.service(claim.service_id) or {}).get("name", claim.service_id)
    prod = config.product(claim.partner_id, claim.product_id) or {}
    view["product"] = {"id": claim.product_id, "name": prod.get("name", claim.product_id),
                       "note": prod.get("note", ""), "handover": prod.get("handover", "")}
    return view


@app.get("/api/claims/{claim_id}/release-code")
def get_release_code(claim_id: str, x_claim_token: str | None = Header(default=None)):
    """수령코드 — 양방향 승인이 모두 유효할 때만, 본인 인증 세션에만 발급."""
    claim = _refresh(_applicant(claim_id, x_claim_token))
    partner = _partner_or_404(claim.partner_id)
    if claim.status == FULFILLED:
        raise HTTPException(409, "이미 수령이 완료된 접수입니다.")
    if claim.status != APPROVED or not claim.release_nonce:
        missing = [
            r for r in claim.required_roles(partner["rules"]) if not claim.approval_valid(r)
        ]
        labels = {STORE: partner["name"], OPERATOR: config.operator()["name"]}
        raise HTTPException(
            409,
            "아직 양방향 인증이 끝나지 않았습니다. 대기 중: "
            + ", ".join(labels.get(r, r) for r in missing),
        )
    period = partner["rules"]["release_code_period_seconds"]
    return {
        "code": security.release_code(claim.id, claim.release_nonce, period),
        "period_seconds": period,
        "seconds_left": security.seconds_left(period),
        "notice": "수령코드는 매 회전마다 바뀌며, 접수자 본인 확인 후에만 사용할 수 있습니다.",
    }


@app.post("/api/claims/{claim_id}/cancel")
def cancel_claim(claim_id: str, x_claim_token: str | None = Header(default=None)):
    claim = _applicant(claim_id, x_claim_token)
    if claim.status in (FULFILLED, CANCELLED):
        raise HTTPException(400, "취소할 수 없는 상태입니다.")
    claim.status = CANCELLED
    claim.release_nonce = ""
    claim.updated_at = now()
    store.save(claim)
    store.audit(claim.id, "cancelled", "applicant", {})
    return {"message": "접수가 취소되었습니다.", "status": claim.status}


# ── 관리자 ────────────────────────────────────────────────
class AdminLogin(BaseModel):
    role: str                      # "store" | "operator"
    partner_id: str | None = None
    password: str


@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    if body.role == OPERATOR:
        expected = config.operator_password_hash()
        name, pid = config.operator()["name"], None
    elif body.role == STORE:
        if not body.partner_id:
            raise HTTPException(400, "제휴처를 선택해 주세요.")
        partner = _partner_or_404(body.partner_id)
        expected = config.partner_password_hash(body.partner_id)
        name, pid = partner["name"], partner["id"]
    else:
        raise HTTPException(400, "알 수 없는 역할입니다.")
    if not expected:
        raise HTTPException(
            403,
            "관리자 비밀번호가 서버에 설정되지 않았습니다. "
            "(DAIEUM_ADMIN_PASSWORD 또는 PARTNER_ADMIN_PASSWORD__<파트너ID>)",
        )
    if not hmac.compare_digest(security.sha256_hex(body.password or ""), expected):
        time.sleep(0.3)
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    return {
        "token": security.issue_token(
            {"t": "admin", "role": body.role, "pid": pid, "admin": name}, ADMIN_TOKEN_TTL
        ),
        "role": body.role,
        "partner_id": pid,
        "name": name,
    }


@app.get("/api/admin/claims")
def admin_list(status: str | None = Query(default=None),
               x_admin_token: str | None = Header(default=None)):
    admin = _admin(x_admin_token)
    scope = None if admin["role"] == OPERATOR else admin["pid"]
    claims = [_refresh(c) for c in store.list_claims(scope)]
    if status:
        claims = [c for c in claims if c.status == status]
    out = []
    for c in claims:
        partner = config.partner(c.partner_id) or {"name": c.partner_id, "rules": {}}
        view = c.admin_view(partner.get("rules", {}), full_identity=admin["role"] == STORE)
        view["partner_name"] = partner["name"]
        prod = config.product(c.partner_id, c.product_id) or {}
        view["product_name"] = prod.get("name", c.product_id)
        view["service_name"] = (config.service(c.service_id) or {}).get("name", c.service_id)
        view["labels"] = _field_labels(partner, config.service(c.service_id))
        out.append(view)
    counts: dict[str, int] = {}
    for c in claims:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {"role": admin["role"], "admin": admin["admin"], "counts": counts, "claims": out}


@app.get("/api/admin/claims/{claim_id}")
def admin_get(claim_id: str, x_admin_token: str | None = Header(default=None)):
    admin = _admin(x_admin_token)
    claim = _refresh(_claim_or_404(claim_id))
    if not _admin_can_see(admin, claim):
        raise HTTPException(403, "다른 제휴처의 접수 건은 볼 수 없습니다.")
    partner = _partner_or_404(claim.partner_id)
    view = claim.admin_view(partner["rules"], full_identity=admin["role"] == STORE)
    view["partner_name"] = partner["name"]
    view["identity_fields"] = partner["identity_fields"]
    view["service"] = config.service(claim.service_id)
    view["labels"] = _field_labels(partner, config.service(claim.service_id))
    view["product"] = config.product(claim.partner_id, claim.product_id)
    view["audit"] = store.audit_trail(claim.id)
    return view


class ApproveBody(BaseModel):
    note: str = ""
    checks: list[str] = []


@app.post("/api/admin/claims/{claim_id}/approve")
def admin_approve(claim_id: str, body: ApproveBody = Body(default=ApproveBody()),
                  x_admin_token: str | None = Header(default=None)):
    """역할별 승인. 두 역할이 모두 승인해야 approved 가 되고 수령코드가 생긴다."""
    admin = _admin(x_admin_token)
    claim = _refresh(_claim_or_404(claim_id))
    if not _admin_can_see(admin, claim):
        raise HTTPException(403, "다른 제휴처의 접수 건은 승인할 수 없습니다.")
    partner = _partner_or_404(claim.partner_id)
    rules = partner["rules"]
    if admin["role"] not in claim.required_roles(rules):
        raise HTTPException(400, "이 제휴처는 해당 역할의 승인을 요구하지 않습니다.")
    if claim.status in (REJECTED, CANCELLED, EXPIRED, PHONE_PENDING):
        raise HTTPException(
            409,
            {
                PHONE_PENDING: "신청자의 휴대폰 본인확인이 아직 끝나지 않았습니다.",
                REJECTED: "이미 반려된 접수입니다.",
                CANCELLED: "취소된 접수입니다.",
                EXPIRED: "유효기간이 지난 접수입니다.",
            }[claim.status],
        )
    if claim.status == FULFILLED:
        raise HTTPException(409, "이미 전달이 완료된 접수입니다.")

    if admin["role"] == STORE:
        # 가게 관리자는 '우리 고객이 맞는지'를 확인한다 — 확인 항목을 기록으로 남긴다.
        required = [f["key"] for f in partner["identity_fields"] if f.get("required")]
        missing = [k for k in required if k not in body.checks]
        if missing:
            labels = {f["key"]: f.get("verify_label") or f.get("label", f["key"])
                      for f in partner["identity_fields"]}
            raise HTTPException(
                400,
                "본인확인 항목을 모두 대조한 뒤 승인해 주세요. 미확인: "
                + ", ".join(labels[k] for k in missing),
            )

    ap = claim.approve(admin["role"], admin["admin"], body.note, body.checks)
    _refresh(claim)
    store.save(claim)
    store.audit(claim.id, f"approved:{admin['role']}", admin["admin"],
                {"note": body.note, "checks": body.checks, "fingerprint": ap.fingerprint[:16]})
    remaining = [r for r in claim.required_roles(rules) if not claim.approval_valid(r)]
    labels = {STORE: partner["name"], OPERATOR: config.operator()["name"]}
    return {
        "status": claim.status,
        "dual_approved": claim.status == APPROVED,
        "waiting_for": [labels.get(r, r) for r in remaining],
        "message": (
            "양방향 인증이 완료되었습니다. 신청자에게 수령코드가 발급됩니다."
            if claim.status == APPROVED
            else "승인했습니다. " + ", ".join(labels.get(r, r) for r in remaining) + " 확인 대기 중입니다."
        ),
    }


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@app.post("/api/admin/claims/{claim_id}/reject")
def admin_reject(claim_id: str, body: RejectBody,
                 x_admin_token: str | None = Header(default=None)):
    admin = _admin(x_admin_token)
    claim = _refresh(_claim_or_404(claim_id))
    if not _admin_can_see(admin, claim):
        raise HTTPException(403, "다른 제휴처의 접수 건은 반려할 수 없습니다.")
    if claim.status == FULFILLED:
        raise HTTPException(409, "이미 전달이 완료된 접수입니다.")
    claim.status = REJECTED
    claim.release_nonce = ""
    claim.rejection = {"role": admin["role"], "admin": admin["admin"],
                       "reason": body.reason, "at": now()}
    claim.updated_at = now()
    store.save(claim)
    store.audit(claim.id, f"rejected:{admin['role']}", admin["admin"], {"reason": body.reason})
    return {"status": claim.status, "message": "반려 처리했습니다."}


class FulfillBody(BaseModel):
    code: str
    phone_last4: str = ""
    note: str = ""


@app.post("/api/admin/claims/{claim_id}/fulfill")
def admin_fulfill(claim_id: str, body: FulfillBody,
                  x_admin_token: str | None = Header(default=None)):
    """상품 전달 확정 — 회전 수령코드 + 휴대폰 뒷자리 대조로 '본인 수령'을 증명."""
    admin = _admin(x_admin_token)
    claim = _refresh(_claim_or_404(claim_id))
    if not _admin_can_see(admin, claim):
        raise HTTPException(403, "다른 제휴처의 접수 건은 처리할 수 없습니다.")
    partner = _partner_or_404(claim.partner_id)
    rules = partner["rules"]
    if claim.status == FULFILLED:
        raise HTTPException(409, "이미 전달이 완료된 접수입니다.")
    if claim.status != APPROVED or not claim.release_nonce:
        raise HTTPException(409, "양방향 인증이 끝나지 않아 전달할 수 없습니다.")
    period = rules["release_code_period_seconds"]
    if not security.release_code_valid(claim.id, claim.release_nonce, body.code, period):
        store.audit(claim.id, "fulfill_code_failed", admin["admin"], {})
        raise HTTPException(400, "수령코드가 올바르지 않거나 만료되었습니다. 신청자 화면의 현재 코드를 확인해 주세요.")
    if rules["pickup_requires_phone_match"]:
        given = re.sub(r"\D", "", body.phone_last4 or "")
        if given != claim.phone_last4:
            store.audit(claim.id, "fulfill_phone_mismatch", admin["admin"], {})
            raise HTTPException(400, "휴대폰 뒷 4자리가 접수자와 일치하지 않습니다. 본인 여부를 확인해 주세요.")

    prod = config.product(claim.partner_id, claim.product_id) or {}
    claim.status = FULFILLED
    claim.fulfillment = {
        "by": admin["admin"], "role": admin["role"], "at": now(), "note": body.note,
        "product_id": claim.product_id, "product_name": prod.get("name", claim.product_id),
        "receipt": security.sign(f"{claim.id}:{claim.release_nonce}:fulfilled".encode())[:16],
    }
    claim.updated_at = now()
    store.save(claim)
    store.audit(claim.id, "fulfilled", admin["admin"],
                {"product": claim.product_id, "receipt": claim.fulfillment["receipt"]})
    return {"status": claim.status, "receipt": claim.fulfillment["receipt"],
            "message": f"{prod.get('name', claim.product_id)} 전달을 확정했습니다."}


@app.get("/api/admin/claims/{claim_id}/audit")
def admin_audit(claim_id: str, x_admin_token: str | None = Header(default=None)):
    admin = _admin(x_admin_token)
    claim = _claim_or_404(claim_id)
    if not _admin_can_see(admin, claim):
        raise HTTPException(403, "다른 제휴처의 기록은 볼 수 없습니다.")
    return {"claim_id": claim_id, "trail": store.audit_trail(claim_id)}


@app.post("/api/admin/reload")
def admin_reload(x_admin_token: str | None = Header(default=None)):
    """제휴처·상품 설정을 다시 읽는다(가게/상품 추가·변경 시)."""
    admin = _admin(x_admin_token)
    if admin["role"] != OPERATOR:
        raise HTTPException(403, "다이음 관리자만 설정을 다시 읽을 수 있습니다.")
    cfg = config.load(force=True)
    return {"message": "제휴 설정을 다시 읽었습니다.",
            "partners": [p["id"] for p in cfg["partners"]],
            "services": list(cfg["services"].keys())}
