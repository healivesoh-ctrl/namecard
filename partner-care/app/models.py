"""접수 건(Claim)의 데이터 모델과 상태 기계.

핵심 규칙
  1) 접수 → 휴대폰 본인확인 → (가게 승인 · 다이음 승인) → 수령코드 → 전달 완료
  2) 두 관리자의 승인은 순서 무관이지만 '둘 다' 있어야 approved 가 된다.
  3) 각 승인은 '접수 지문(fingerprint)'에 대한 서명이다. 신청자 정보가 나중에
     바뀌면 지문이 달라져 승인이 자동 무효화된다 → 접수 후 명의 양도 차단.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import security

# 상태
PHONE_PENDING = "phone_pending"   # 접수됨, 휴대폰 본인확인 대기
PENDING = "pending"               # 본인확인 완료, 관리자 승인 대기
APPROVED = "approved"             # 양방향 승인 완료 → 수령코드 발급
FULFILLED = "fulfilled"           # 상품 전달 완료
REJECTED = "rejected"
CANCELLED = "cancelled"
EXPIRED = "expired"

STORE = "store"        # 가게(예: 대추밭한의원) 관리자
OPERATOR = "operator"  # 디에이블 다이음 관리자


def now() -> int:
    return int(time.time())


def new_id() -> str:
    """사람이 읽고 부르기 쉬운 접수번호."""
    return f"C{time.strftime('%y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


@dataclass
class Approval:
    role: str
    admin: str
    at: int
    note: str = ""
    checks: list[str] = field(default_factory=list)  # 확인한 본인확인 항목 key
    fingerprint: str = ""                            # 승인 시점의 접수 지문
    signature: str = ""                              # 지문에 대한 HMAC 서명


@dataclass
class Claim:
    id: str
    partner_id: str
    service_id: str
    product_id: str
    status: str

    # 신청자 (전화번호는 원문 대신 마스킹 + 해시로 보관)
    applicant_name: str
    phone_masked: str
    phone_key: str
    phone_last4: str

    identity: dict = field(default_factory=dict)   # 가게별 본인확인 입력값
    booking: dict = field(default_factory=dict)    # 서비스 예약 정보
    consent: dict = field(default_factory=dict)

    created_at: int = 0
    updated_at: int = 0
    expires_at: int = 0

    approvals: dict = field(default_factory=dict)  # role -> Approval(dict)
    rejection: dict = field(default_factory=dict)
    release_nonce: str = ""                        # 양방향 승인 완료 시에만 채워짐
    fulfillment: dict = field(default_factory=dict)

    otp: dict = field(default_factory=dict)        # {digest, expires_at, attempts, sent_at}

    # ── 지문 / 승인 ────────────────────────────────────────
    def fingerprint(self) -> str:
        """신청자 동일성을 규정하는 값들의 해시. 하나라도 바뀌면 승인이 깨진다."""
        canonical = json.dumps(
            {
                "id": self.id,
                "partner_id": self.partner_id,
                "service_id": self.service_id,
                "product_id": self.product_id,
                "applicant_name": self.applicant_name.strip(),
                "phone_key": self.phone_key,
                "identity": {k: str(v).strip() for k, v in sorted(self.identity.items())},
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def approve(self, role: str, admin: str, note: str = "", checks: list[str] | None = None) -> Approval:
        fp = self.fingerprint()
        ap = Approval(
            role=role, admin=admin, at=now(), note=note, checks=checks or [],
            fingerprint=fp, signature=security.sign(f"{role}:{admin}:{fp}".encode()),
        )
        self.approvals[role] = asdict(ap)
        self.updated_at = now()
        return ap

    def approval_valid(self, role: str) -> bool:
        """승인 서명이 위조되지 않았고, 현재 접수 내용과 지문이 일치하는가."""
        ap = self.approvals.get(role)
        if not ap:
            return False
        expected = security.sign(f"{ap['role']}:{ap['admin']}:{ap['fingerprint']}".encode())
        return ap["signature"] == expected and ap["fingerprint"] == self.fingerprint()

    def required_roles(self, rules: dict) -> list[str]:
        roles = []
        if rules.get("require_store_approval", True):
            roles.append(STORE)
        if rules.get("require_daieum_approval", True):
            roles.append(OPERATOR)
        return roles

    def dual_approved(self, rules: dict) -> bool:
        return all(self.approval_valid(r) for r in self.required_roles(rules))

    def refresh_status(self, rules: dict) -> str:
        """승인 상태로부터 status 를 다시 계산한다(단일 진실 공급원)."""
        if self.status in (REJECTED, CANCELLED, FULFILLED):
            return self.status
        if self.expires_at and now() > self.expires_at and self.status != APPROVED:
            self.status = EXPIRED
            return self.status
        if self.status == PHONE_PENDING:
            return self.status
        if self.dual_approved(rules):
            if not self.release_nonce:
                # 수령코드의 씨앗은 '양방향 승인이 모두 유효한 순간'에만 만들어진다.
                self.release_nonce = security.sign(
                    ":".join(
                        self.approvals[r]["signature"] for r in sorted(self.required_roles(rules))
                    ).encode()
                )
            self.status = APPROVED
        else:
            # 지문이 깨져 승인이 무효화된 경우 다시 심사 대기로 되돌린다.
            self.release_nonce = ""
            self.status = PENDING
        return self.status

    # ── 직렬화 ────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # ── 노출용 뷰 ─────────────────────────────────────────
    def public_view(self, rules: dict) -> dict:
        """고객 화면용. 개인정보 원문·OTP·nonce 는 제외."""
        return {
            "id": self.id,
            "partner_id": self.partner_id,
            "service_id": self.service_id,
            "product_id": self.product_id,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "applicant_name": security.mask_name(self.applicant_name),
            "phone_masked": self.phone_masked,
            "booking": self.booking,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "approvals": {
                role: {
                    "role": role,
                    "admin": ap["admin"],
                    "at": ap["at"],
                    "valid": self.approval_valid(role),
                }
                for role, ap in self.approvals.items()
            },
            "required_roles": self.required_roles(rules),
            "rejection": self.rejection,
            "fulfillment": {k: v for k, v in self.fulfillment.items() if k != "code"},
            "transferable": rules.get("transfer_allowed", False),
        }

    def admin_view(self, rules: dict, full_identity: bool) -> dict:
        """관리자 화면용. 가게 관리자에게만 본인확인 입력값 원문을 보여준다."""
        view = self.public_view(rules)
        view["applicant_name"] = self.applicant_name
        view["phone_last4"] = self.phone_last4
        view["identity"] = self.identity if full_identity else {"_": "가게 관리자 전용"}
        view["consent"] = self.consent
        view["fingerprint"] = self.fingerprint()[:16]
        view["approvals"] = {
            role: {**ap, "valid": self.approval_valid(role), "signature": ap["signature"][:12] + "…"}
            for role, ap in self.approvals.items()
        }
        return view


STATUS_LABELS = {
    PHONE_PENDING: "휴대폰 본인확인 대기",
    PENDING: "관리자 승인 대기",
    APPROVED: "양방향 인증 완료 — 수령 가능",
    FULFILLED: "상품 전달 완료",
    REJECTED: "반려됨",
    CANCELLED: "취소됨",
    EXPIRED: "기한 만료",
}
