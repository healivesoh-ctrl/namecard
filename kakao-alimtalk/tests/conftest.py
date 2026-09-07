import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alimtalk.config import parse_config  # noqa: E402
from alimtalk.store import Store  # noqa: E402

RULES = {
    "provider": "console",
    "sender": "0212345678",
    "timezone": "Asia/Seoul",
    "quiet_hours": {"start": "21:00", "end": "08:00"},
    "forms": [
        {
            "id": "default",
            "dedupe_hours": 24,
            "messages": [
                {"name": "접수확인", "template": "TP1", "when": "immediate",
                 "variables": {"이름": "{{name|고객}}", "과정명": "{{수강 과정|프로그램}}"},
                 "text": "{{name|고객}}님, {{수강 과정|프로그램}} 접수되었습니다."},
                {"name": "후속안내", "template": "TP2", "when": {"delay": "1d"},
                 "variables": {"이름": "{{name}}"}, "text": "{{name}}님 후속"},
                {"name": "리마인드", "template": "TP3",
                 "when": {"field_at": {"field": "수업일", "days": -1, "time": "18:00"}},
                 "text": "{{name}}님 내일 {{수업일}}"},
                {"name": "무통장", "template": "TP4", "when": {"delay": "5m"},
                 "match": {"결제 방법": "무통장"}, "text": "계좌 안내"},
            ],
        },
        {"id": "consent-form", "require_consent": True,
         "messages": [{"name": "광고", "template": "TP9", "text": "(광고) 안내"}]},
    ],
}


@pytest.fixture
def cfg():
    return parse_config(RULES)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("ALIMTALK_PROVIDER", "ALIMTALK_SENDER", "ALIMTALK_DRY_RUN", "KAKAO_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
