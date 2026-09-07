from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))  # 야간금지 시간대 밖

from alimtalk.models import SendResult
from alimtalk.providers.console import ConsoleProvider
from alimtalk.service import MAX_ATTEMPTS, deliver, intake, run_due


class FailingProvider:
    name = "fail"

    def __init__(self):
        self.calls = 0

    def send(self, msg):
        self.calls += 1
        return SendResult(ok=False, provider=self.name, error="boom")


def test_intake_creates_jobs(cfg, store):
    r = intake(cfg, store, "default", {"이름": "홍길동", "연락처": "010-1234-5678", "수강 과정": "AI"})
    assert r.accepted and r.phone == "01012345678"
    assert [j.message_name for j in r.jobs] == ["접수확인", "후속안내"]
    assert store.counts()["pending"] == 2


def test_intake_dedupe_optout_consent(cfg, store):
    a = {"이름": "홍길동", "연락처": "010-1234-5678"}
    assert intake(cfg, store, "default", a).accepted
    r = intake(cfg, store, "default", a)
    assert not r.accepted and "중복" in r.reason
    store.add_optout("01012345678")
    assert store.counts().get("cancelled") == 2  # 대기 중 예약 취소
    r = intake(cfg, store, "default", {"이름": "김", "연락처": "01012345678"})
    assert not r.accepted and "수신거부" in r.reason
    r = intake(cfg, store, "consent-form", {"연락처": "010-2222-3333"})
    assert not r.accepted and "동의" in r.reason
    r = intake(cfg, store, "consent-form", {"연락처": "010-2222-3333", "수신동의": "동의"})
    assert r.accepted
    r = intake(cfg, store, "default", {"연락처": "010-4444-3333", "마케팅 수신동의": "동의하지 않음"})
    assert not r.accepted and "비동의" in r.reason
    # 모르는 폼 id 는 default 규칙으로 처리
    r = intake(cfg, store, "nope", {"연락처": "010-5555-3333"})
    assert r.accepted and r.jobs[0].form_id == "default"
    r = intake(cfg, store, "default", {"이름": "번호없음"})
    assert not r.accepted and "번호" in r.reason


def test_run_due_and_retry(cfg, store):
    now = NOW
    intake(cfg, store, "default", {"이름": "홍길동", "연락처": "010-1234-5678"}, now=now)
    results = run_due(cfg, store, ConsoleProvider(), now)
    assert len(results) == 1 and results[0][1].ok
    assert store.counts()["sent"] == 1 and store.counts()["pending"] == 1
    # 1일 뒤 작업은 아직 아님
    assert run_due(cfg, store, ConsoleProvider(), now + timedelta(hours=23)) == []
    assert len(run_due(cfg, store, ConsoleProvider(), now + timedelta(days=1, minutes=1))) == 1


def test_retry_then_fail(cfg, store):
    now = NOW
    r = intake(cfg, store, "default", {"연락처": "010-1234-5678"}, now=now)
    job = r.jobs[0]
    p = FailingProvider()
    for i in range(MAX_ATTEMPTS):
        res = deliver(cfg, store, p, job, now)
        assert not res.ok
        if i < MAX_ATTEMPTS - 1:
            assert job.status == "pending" and job.scheduled_at > now
    assert job.status == "failed" and p.calls == MAX_ATTEMPTS


def test_deliver_requires_sender(store):
    from alimtalk.config import parse_config
    from alimtalk.models import Job
    cfg = parse_config({"forms": [{"id": "default", "messages": []}]})
    job = store.add_job(Job(None, None, "default", "m", "01012345678", "TP", {}, "t", None, datetime.now(timezone.utc)))
    res = deliver(cfg, store, FailingProvider(), job)
    assert not res.ok and "발신번호" in res.error
