"""응답 접수 → 작업 예약 → 발송 실행 (라이브러리 진입점)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .models import Job, OutboundMessage, SendResult, Submission
from .providers import Provider
from .rules import SkipSubmission, build_jobs, extract
from .store import Store

log = logging.getLogger("alimtalk")

MAX_ATTEMPTS = 3
RETRY_MINUTES = (2, 10, 30)


@dataclass
class IntakeResult:
    accepted: bool
    reason: str = ""
    submission_id: int | None = None
    phone: str = ""
    jobs: list[Job] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted, "reason": self.reason, "submission_id": self.submission_id,
            "jobs": [{"id": j.id, "message": j.message_name, "scheduled_at": j.scheduled_at.isoformat()} for j in self.jobs],
            "skipped": self.skipped,
        }


def intake(cfg: Config, store: Store, form_id: str, answers: dict[str, Any], *,
           source: str = "webhook", phone_hint: str | None = None, name_hint: str | None = None,
           now: datetime | None = None) -> IntakeResult:
    """폼 응답 1건을 받아 발송 작업을 예약한다. 발송 자체는 워커가 수행."""
    answers = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in (answers or {}).items()}
    form = cfg.form(form_id)
    try:
        ext = extract(cfg, form, answers, phone_hint, name_hint)
    except SkipSubmission as e:
        return IntakeResult(False, str(e))
    if form is None:
        return IntakeResult(False, f"rules.yaml 에 폼 '{form_id}' 규칙이 없습니다", phone=ext.phone)
    if store.is_opted_out(ext.phone):
        return IntakeResult(False, "수신거부 번호", phone=ext.phone)
    if form.require_consent and not ext.consent:
        return IntakeResult(False, "수신 동의 없음", phone=ext.phone)
    if ext.consent is False:
        return IntakeResult(False, "수신 비동의 응답", phone=ext.phone)
    if store.recent_submission(form.id, ext.phone, form.dedupe_hours):
        return IntakeResult(False, f"{form.dedupe_hours:g}시간 내 중복 응답", phone=ext.phone)

    sub = store.add_submission(Submission(form_id=form.id, phone=ext.phone, name=ext.name,
                                          answers=answers, source=source,
                                          received_at=now or datetime.now(timezone.utc)))
    try:
        jobs, skipped = build_jobs(cfg, sub, now)
    except SkipSubmission as e:
        return IntakeResult(False, str(e), submission_id=sub.id, phone=ext.phone)
    for j in jobs:
        store.add_job(j)
    log.info("접수 form=%s phone=***%s jobs=%d skipped=%s", form.id, ext.phone[-4:], len(jobs), skipped)
    return IntakeResult(True, submission_id=sub.id, phone=ext.phone, jobs=jobs, skipped=skipped)


def to_outbound(cfg: Config, job: Job, fallback: bool = True) -> OutboundMessage:
    return OutboundMessage(to=job.phone, sender=cfg.sender, template_code=job.template_code,
                           variables=job.variables, text=job.text, subject=job.subject, fallback=fallback)


def deliver(cfg: Config, store: Store, provider: Provider, job: Job,
            now: datetime | None = None) -> SendResult:
    """작업 1건 발송 시도 후 상태 갱신. 실패 시 최대 MAX_ATTEMPTS 회 재시도 예약."""
    now = now or datetime.now(timezone.utc)
    if store.is_opted_out(job.phone):
        job.status = "cancelled"
        job.result = {"error": "수신거부 번호"}
        store.update_job(job)
        return SendResult(ok=False, provider=provider.name, error="수신거부 번호")
    if not cfg.sender and provider.name != "console":
        res = SendResult(ok=False, provider=provider.name, error="발신번호(ALIMTALK_SENDER)가 설정되지 않았습니다")
    else:
        try:
            res = provider.send(to_outbound(cfg, job))
        except Exception as e:  # noqa: BLE001 - 발송사 예외는 모두 실패로 기록
            log.exception("발송 중 예외")
            res = SendResult(ok=False, provider=provider.name, error=f"예외: {e}")
    job.attempts += 1
    job.result = res.to_dict()
    if res.ok:
        job.status = "sent"
        job.sent_at = now
    elif job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
    else:
        from datetime import timedelta
        job.scheduled_at = now + timedelta(minutes=RETRY_MINUTES[min(job.attempts - 1, len(RETRY_MINUTES) - 1)])
    store.update_job(job)
    return res


def run_due(cfg: Config, store: Store, provider: Provider, now: datetime | None = None,
            limit: int = 50) -> list[tuple[Job, SendResult]]:
    """발송 시각이 된 작업을 모두 처리한다 (워커·cron 에서 호출)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for job in store.due_jobs(now, limit):
        out.append((job, deliver(cfg, store, provider, job, now)))
    return out
