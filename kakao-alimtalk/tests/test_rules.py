from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from alimtalk.config import parse_config
from alimtalk.models import Submission
from alimtalk.rules import (SkipSubmission, apply_quiet_hours, build_jobs, compute_send_time,
                            extract, is_affirmative, matches, parse_date, parse_delay)
from alimtalk.templates import find_field, render

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=KST)


def test_parse_delay():
    assert parse_delay("30m") == timedelta(minutes=30)
    assert parse_delay("2h") == timedelta(hours=2)
    assert parse_delay("1d") == timedelta(days=1)
    assert parse_delay("1주") == timedelta(weeks=1)
    with pytest.raises(ValueError):
        parse_delay("soon")


@pytest.mark.parametrize("s,ymd", [
    ("2026-09-20", (2026, 9, 20)), ("2026.9.20", (2026, 9, 20)), ("2026/09/20", (2026, 9, 20)),
    ("2026. 9. 20.", (2026, 9, 20)), ("20260920", (2026, 9, 20)), ("2026년 9월 20일", (2026, 9, 20)),
])
def test_parse_date(s, ymd):
    d = parse_date(s)
    assert (d.year, d.month, d.day) == ymd


def test_parse_date_invalid():
    assert parse_date("다음주") is None
    assert parse_date("2026-13-40") is None


def test_quiet_hours():
    q = ("21:00", "08:00")
    late = datetime(2026, 9, 7, 22, 30, tzinfo=KST)
    assert apply_quiet_hours(late, q) == datetime(2026, 9, 8, 8, 0, tzinfo=KST)
    early = datetime(2026, 9, 7, 3, 0, tzinfo=KST)
    assert apply_quiet_hours(early, q) == datetime(2026, 9, 7, 8, 0, tzinfo=KST)
    day = datetime(2026, 9, 7, 14, 0, tzinfo=KST)
    assert apply_quiet_hours(day, q) == day
    assert apply_quiet_hours(late, None) == late


def test_compute_send_time():
    assert compute_send_time("immediate", NOW, {}) == NOW
    assert compute_send_time({"delay": "2h"}, NOW, {}) == NOW + timedelta(hours=2)
    assert compute_send_time("1d", NOW, {}) == NOW + timedelta(days=1)
    assert compute_send_time({"at": "2026-09-20 09:00"}, NOW, {}) == datetime(2026, 9, 20, 9, 0, tzinfo=KST)
    # 과거 일시는 즉시
    assert compute_send_time({"at": "2026-01-01 09:00"}, NOW, {}) == NOW
    # 다음 09:00 → 오늘 10시 이후이므로 내일
    assert compute_send_time({"at_time": "09:00"}, NOW, {}) == datetime(2026, 9, 8, 9, 0, tzinfo=KST)
    assert compute_send_time({"at_time": "15:30"}, NOW, {}) == datetime(2026, 9, 7, 15, 30, tzinfo=KST)
    # 폼 날짜 기준 D-1 18:00
    t = compute_send_time({"field_at": {"field": "수업일", "days": -1, "time": "18:00"}}, NOW, {"수업일": "2026.09.20"})
    assert t == datetime(2026, 9, 19, 18, 0, tzinfo=KST)
    with pytest.raises(SkipSubmission):
        compute_send_time({"field_at": {"field": "수업일", "days": -1}}, NOW, {"수업일": "미정"})
    with pytest.raises(SkipSubmission):
        compute_send_time({"field_at": {"field": "수업일", "days": -1}}, NOW, {"수업일": "2026-01-01"})
    # 야간 금지 적용
    t = compute_send_time({"delay": "12h"}, NOW, {}, ("21:00", "08:00"))
    assert t == datetime(2026, 9, 8, 8, 0, tzinfo=KST)


def test_find_field_and_render():
    answers = {"타임스탬프": "2026-09-07", "성함을 적어주세요": "홍길동", "휴대폰 번호": "010-1234-5678", "수강 과정": "AI 사고력"}
    assert find_field(answers, ["이름", "성함"]) == "성함을 적어주세요"
    assert find_field(answers, ["전화번호", "휴대폰"]) == "휴대폰 번호"
    assert find_field(answers, ["없는항목"]) is None
    assert render("{{성함}}님 {{수강 과정}} / {{없음|기본}}", answers) == "홍길동님 AI 사고력 / 기본"
    assert render("{{name}}", answers, {"name": "김철수"}) == "김철수"


def test_extract(cfg):
    ex = extract(cfg, cfg.form("default"), {"이름": "홍길동", "연락처": "010-1234-5678", "수신동의": "동의합니다"})
    assert (ex.phone, ex.name, ex.consent) == ("01012345678", "홍길동", True)
    ex = extract(cfg, cfg.form("default"), {"이름": "홍길동", "연락처": "010-1234-5678"})
    assert ex.consent is None
    # 항목명이 특이해도 값에서 번호를 찾는다
    ex = extract(cfg, cfg.form("default"), {"q1": "홍길동", "q2": "010-9999-8888"})
    assert ex.phone == "01099998888"
    with pytest.raises(SkipSubmission):
        extract(cfg, cfg.form("default"), {"이름": "홍길동", "연락처": "02-123-4567"})


def test_is_affirmative():
    assert is_affirmative("동의합니다")
    assert is_affirmative("예")
    assert is_affirmative("Yes")
    assert not is_affirmative("동의하지 않습니다")
    assert not is_affirmative("비동의")
    assert not is_affirmative("")


def test_matches():
    a = {"결제 방법": "무통장 입금", "과정": "AI"}
    assert matches({"결제 방법": "무통장"}, a)
    assert matches({"결제 방법": ["카드", "무통장"]}, a)
    assert not matches({"결제 방법": "카드"}, a)
    assert matches({}, a)


def test_build_jobs(cfg):
    sub = Submission(form_id="default", phone="01012345678", name="홍길동", id=1,
                     answers={"수강 과정": "AI 사고력", "수업일": "2026-09-20", "결제 방법": "카드"})
    jobs, skipped = build_jobs(cfg, sub, NOW)
    names = [j.message_name for j in jobs]
    assert names == ["접수확인", "후속안내", "리마인드"]
    assert skipped == ["무통장: 조건 불일치"]
    j0 = jobs[0]
    assert j0.variables == {"이름": "홍길동", "과정명": "AI 사고력"}
    assert j0.text == "홍길동님, AI 사고력 접수되었습니다."
    assert j0.scheduled_at == NOW
    assert jobs[1].scheduled_at == NOW + timedelta(days=1)
    assert jobs[2].scheduled_at == datetime(2026, 9, 19, 18, 0, tzinfo=KST)


def test_build_jobs_defaults(cfg):
    sub = Submission(form_id="default", phone="01012345678", name="", id=1, answers={})
    jobs, skipped = build_jobs(cfg, sub, NOW)
    assert jobs[0].variables == {"이름": "고객", "과정명": "프로그램"}
    assert any("리마인드" in s for s in skipped)


def test_form_lookup():
    c = parse_config({"forms": [{"id": "a", "messages": []}]})
    assert c.form("a").id == "a"
    assert c.form(None).id == "a"
    assert c.form("zzz") is None
    c2 = parse_config({"forms": [{"id": "default", "messages": []}, {"id": "b", "messages": []}]})
    assert c2.form("zzz").id == "default"
