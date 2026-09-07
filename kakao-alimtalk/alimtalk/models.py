"""데이터 모델."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class OutboundMessage:
    """발송사(Provider)에 넘기는 메시지 1건."""

    to: str                      # 수신번호 (01012345678)
    sender: str                  # 발신번호 (사전 등록된 번호)
    template_code: str           # 알림톡 템플릿 코드
    variables: dict[str, str] = field(default_factory=dict)  # 템플릿 변수 (이름 → 값)
    text: str | None = None      # 템플릿 변수를 치환한 완성 본문 (알리고 필수, 솔라피는 SMS 대체 발송문)
    subject: str | None = None   # 제목 (알리고 / LMS 대체 발송 시)
    fallback: bool = True        # 알림톡 실패 시 문자(SMS/LMS)로 대체 발송
    buttons: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    provider: str
    channel: str = "alimtalk"    # alimtalk | sms | dry-run
    message_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Submission:
    """설문폼 응답 1건 (정규화 후)."""

    form_id: str
    phone: str
    answers: dict[str, str]
    name: str = ""
    received_at: datetime | None = None
    source: str = "webhook"
    id: int | None = None


@dataclass
class Job:
    """예약된 발송 1건."""

    id: int | None
    submission_id: int | None
    form_id: str
    message_name: str
    phone: str
    template_code: str
    variables: dict[str, str]
    text: str | None
    subject: str | None
    scheduled_at: datetime
    status: str = "pending"      # pending | sent | failed | cancelled | skipped
    attempts: int = 0
    result: dict[str, Any] | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None
