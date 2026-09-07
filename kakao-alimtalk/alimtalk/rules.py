"""폼 응답 → 발송 작업(Job) 생성 규칙.

- 항목 자동 인식: 전화번호·이름·수신동의 항목을 이름 키워드로 찾는다.
- 조건(match): 특정 답변일 때만 발송.
- 시점(when): 즉시 / N분·시간·일 뒤 / 특정 일시 / 다음 HH:MM / 폼의 날짜 항목 기준 D-n.
- 야간 발송 금지(quiet_hours): 해당 시간대면 종료 시각으로 미룬다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import AFFIRMATIVE, Config, FormRule, MessageRule
from .models import Job, Submission
from .phone import normalize_phone
from .templates import builtin_vars, find_field, lookup, render, render_variables

_DELAY = re.compile(r"^\s*(\d+)\s*([mhdw]|분|시간|일|주)\s*$", re.I)
_UNIT = {"m": "minutes", "분": "minutes", "h": "hours", "시간": "hours",
         "d": "days", "일": "days", "w": "weeks", "주": "weeks"}


class SkipSubmission(Exception):
    """이 응답에는 발송하지 않음 (사유 포함)."""


@dataclass
class ExtractResult:
    phone: str
    name: str
    consent: bool | None      # None = 동의 항목이 폼에 없음


def parse_delay(s: str) -> timedelta:
    m = _DELAY.match(str(s))
    if not m:
        raise ValueError(f"지연 시간 형식이 잘못됐습니다: {s!r} (예: 30m, 2h, 1d)")
    return timedelta(**{_UNIT[m.group(2).lower()]: int(m.group(1))})


def parse_hhmm(s: str) -> time:
    hh, mm = str(s).strip().split(":")[:2]
    return time(int(hh), int(mm))


def parse_date(s: str) -> date | None:
    """'2026-09-20', '2026.9.20', '2026/09/20', '2026. 9. 20.', '20260920', '9월 20일' 인식."""
    s = str(s).strip()
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    elif re.fullmatch(r"\d{8}", s):
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
    else:
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
        if not m:
            return None
        y, mo, d = date.today().year, int(m.group(1)), int(m.group(2))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def is_affirmative(v: Any) -> bool:
    s = str(v or "").strip().lower()
    if not s:
        return False
    if any(neg in s for neg in ("비동의", "미동의", "동의하지", "거부", "no", "아니")):
        return False
    return any(a in s for a in AFFIRMATIVE)


def extract(cfg: Config, form: FormRule | None, answers: dict[str, str],
            phone_hint: str | None = None, name_hint: str | None = None) -> ExtractResult:
    """응답에서 전화번호·이름·수신동의를 뽑는다. 전화번호가 없으면 SkipSubmission."""
    phone = normalize_phone(phone_hint) if phone_hint else None
    if not phone:
        key = find_field(answers, cfg.field_keywords(form, "phone"))
        if key:
            phone = normalize_phone(answers[key])
        if not phone:  # 항목명이 특이한 경우: 값 중 휴대폰 번호처럼 생긴 것을 찾는다
            for v in answers.values():
                phone = normalize_phone(v)
                if phone:
                    break
    if not phone:
        raise SkipSubmission("휴대폰 번호를 찾지 못했습니다")
    name = (name_hint or "").strip()
    if not name:
        key = find_field(answers, cfg.field_keywords(form, "name"))
        name = str(answers[key]).strip() if key else ""
    consent: bool | None = None
    ckey = find_field(answers, cfg.field_keywords(form, "consent"))
    if ckey:
        consent = is_affirmative(answers[ckey])
    return ExtractResult(phone=phone, name=name, consent=consent)


def matches(cond: dict[str, Any], answers: dict[str, str]) -> bool:
    """{항목: 값} 조건. 값이 리스트면 그중 하나, 문자열이면 부분 일치(대소문자 무시)."""
    for key, want in (cond or {}).items():
        got = lookup(answers, key).lower()
        wants = want if isinstance(want, list) else [want]
        if not any(str(w).lower() in got for w in wants):
            return False
    return True


def in_quiet_hours(t: datetime, quiet: tuple[str, str] | None) -> bool:
    if not quiet:
        return False
    start, end = parse_hhmm(quiet[0]), parse_hhmm(quiet[1])
    now = t.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # 자정을 넘는 구간 (21:00 ~ 08:00)


def apply_quiet_hours(t: datetime, quiet: tuple[str, str] | None) -> datetime:
    if not in_quiet_hours(t, quiet):
        return t
    end = parse_hhmm(quiet[1])
    candidate = t.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= t:
        candidate += timedelta(days=1)
    return candidate


def compute_send_time(when: Any, now: datetime, answers: dict[str, str],
                      quiet: tuple[str, str] | None = None) -> datetime:
    """규칙의 when 을 실제 발송 시각으로 계산한다 (now 는 tz-aware)."""
    tz = now.tzinfo
    if when in (None, "", "immediate", "now", "즉시"):
        t = now
    elif isinstance(when, str):
        t = now + parse_delay(when)
    elif isinstance(when, dict):
        if "delay" in when:
            t = now + parse_delay(when["delay"])
        elif "at" in when:
            t = datetime.strptime(str(when["at"]), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        elif "at_time" in when:
            tt = parse_hhmm(when["at_time"])
            t = now.replace(hour=tt.hour, minute=tt.minute, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
        elif "field_at" in when:
            spec = when["field_at"]
            key = find_field(answers, [spec.get("field", "")])
            d = parse_date(answers[key]) if key else None
            if not d:
                raise SkipSubmission(f"날짜 항목({spec.get('field')})을 읽지 못해 예약을 건너뜁니다")
            tt = parse_hhmm(spec.get("time", "09:00"))
            t = datetime.combine(d, tt, tzinfo=tz) + timedelta(days=int(spec.get("days", 0)))
            if t <= now:
                raise SkipSubmission("예약 시각이 이미 지났습니다")
        else:
            raise ValueError(f"알 수 없는 when 설정: {when}")
    else:
        raise ValueError(f"알 수 없는 when 설정: {when}")
    if t < now:
        t = now
    return apply_quiet_hours(t, quiet)


def build_jobs(cfg: Config, sub: Submission, now: datetime | None = None) -> tuple[list[Job], list[str]]:
    """응답 1건에 대해 규칙에 맞는 발송 작업 목록을 만든다. 반환: (jobs, 건너뛴 사유들)"""
    tz = ZoneInfo(cfg.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    form = cfg.form(sub.form_id)
    if form is None:
        raise SkipSubmission(f"rules.yaml 에 폼 '{sub.form_id}' 규칙이 없습니다")
    if not matches(form.match, sub.answers):
        raise SkipSubmission("폼 조건(match)에 맞지 않는 응답")
    builtins = builtin_vars(sub.name, sub.phone, now)
    jobs: list[Job] = []
    skipped: list[str] = []
    for m in form.messages:
        if not matches(m.match, sub.answers):
            skipped.append(f"{m.name}: 조건 불일치")
            continue
        try:
            at = compute_send_time(m.when, now, sub.answers, cfg.quiet_hours)
        except SkipSubmission as e:
            skipped.append(f"{m.name}: {e}")
            continue
        jobs.append(Job(
            id=None, submission_id=sub.id, form_id=form.id, message_name=m.name,
            phone=sub.phone, template_code=m.template,
            variables=render_variables(m.variables, sub.answers, builtins),
            text=render(m.text, sub.answers, builtins),
            subject=render(m.subject, sub.answers, builtins),
            scheduled_at=at,
        ))
    return jobs, skipped
