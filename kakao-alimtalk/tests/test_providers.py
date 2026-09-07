import json

import pytest

from alimtalk.models import OutboundMessage
from alimtalk.providers import ConsoleProvider, ProviderError, get_provider
from alimtalk.providers.aligo import AligoProvider
from alimtalk.providers.solapi import SolapiProvider

MSG = OutboundMessage(to="01012345678", sender="0212345678", template_code="TP1",
                      variables={"이름": "홍길동"}, text="홍길동님 안내", subject="안내")


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def test_get_provider_env(monkeypatch):
    assert isinstance(get_provider("console"), ConsoleProvider)
    monkeypatch.setenv("ALIMTALK_DRY_RUN", "1")
    assert isinstance(get_provider("solapi"), ConsoleProvider)
    monkeypatch.delenv("ALIMTALK_DRY_RUN")
    with pytest.raises(ProviderError):
        get_provider("solapi")  # 키 없음
    with pytest.raises(ProviderError):
        get_provider("unknown")


def test_solapi_payload_and_send(monkeypatch):
    p = SolapiProvider(api_key="k", api_secret="s", pf_id="KA01PF")
    payload = p.build_payload(MSG)
    m = payload["message"]
    assert m["to"] == "01012345678" and m["type"] == "ATA" and m["text"] == "홍길동님 안내"
    assert m["kakaoOptions"]["variables"] == {"#{이름}": "홍길동"}
    assert m["kakaoOptions"]["disableSms"] is False
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"], captured["auth"] = url, headers["Authorization"]
        return FakeResp(200, {"messageId": "M1", "statusCode": "2000", "statusMessage": "정상 접수"})

    monkeypatch.setattr("alimtalk.providers.solapi.requests.post", fake_post)
    res = p.send(MSG)
    assert res.ok and res.message_id == "M1"
    assert captured["url"].endswith("/messages/v4/send")
    assert captured["auth"].startswith("HMAC-SHA256 apiKey=k, date=") and "signature=" in captured["auth"]

    monkeypatch.setattr("alimtalk.providers.solapi.requests.post",
                        lambda *a, **k: FakeResp(400, {"errorCode": "ValidationError", "errorMessage": "bad"}))
    res = p.send(MSG)
    assert not res.ok and "bad" in res.error
    monkeypatch.setattr("alimtalk.providers.solapi.requests.post",
                        lambda *a, **k: FakeResp(200, {"statusCode": "4000", "statusMessage": "수신번호 오류"}))
    assert not p.send(MSG).ok


def test_aligo_form_and_send(monkeypatch):
    p = AligoProvider(api_key="k", user_id="u", sender_key="SK", test_mode=True)
    form = p.build_form(MSG, "TOKEN")
    assert form["receiver_1"] == "01012345678" and form["message_1"] == "홍길동님 안내"
    assert form["tpl_code"] == "TP1" and form["testMode"] == "Y" and form["failover"] == "Y"
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(url)
        if "token" in url:
            return FakeResp(200, {"code": 0, "token": "TOKEN"})
        return FakeResp(200, {"code": 0, "message": "성공", "info": {"type": "AT", "mid": 123}})

    monkeypatch.setattr("alimtalk.providers.aligo.requests.post", fake_post)
    res = p.send(MSG)
    assert res.ok and res.message_id == "123" and len(calls) == 2

    # 본문 없으면 알리고는 실패
    res = p.send(OutboundMessage(to="01012345678", sender="02", template_code="TP1"))
    assert not res.ok and "본문" in res.error

    monkeypatch.setattr("alimtalk.providers.aligo.requests.post",
                        lambda url, **k: FakeResp(200, {"code": -101, "message": "인증오류"}))
    res = p.send(MSG)
    assert not res.ok and "인증오류" in res.error
