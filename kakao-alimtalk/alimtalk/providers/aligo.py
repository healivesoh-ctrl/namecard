"""알리고(aligo) 카카오 알림톡 발송.

준비: 알리고 가입 → 카카오 채널 연동(senderkey 발급) → 템플릿 등록·승인 → API Key 발급.
알리고는 템플릿 변수를 서버에서 치환하지 않으므로 **완성된 본문(message)** 을 보내야 한다.
따라서 rules.yaml 의 `text` 항목이 필수이며, 승인된 템플릿 원문과 변수 부분 외에는 같아야 한다.
문서: https://smartsms.aligo.in/admin/api/kakao.html
"""

from __future__ import annotations

import json
import os

import requests

from ..models import OutboundMessage, SendResult
from .base import ProviderError

API_BASE = "https://kakaoapi.aligo.in/akv10"


class AligoProvider:
    name = "aligo"

    def __init__(self, api_key: str | None = None, user_id: str | None = None,
                 sender_key: str | None = None, test_mode: bool | None = None, timeout: float = 15):
        self.api_key = api_key or os.environ.get("ALIGO_API_KEY", "")
        self.user_id = user_id or os.environ.get("ALIGO_USER_ID", "")
        self.sender_key = sender_key or os.environ.get("ALIGO_SENDER_KEY") or os.environ.get("KAKAO_CHANNEL_ID", "")
        self.test_mode = (os.environ.get("ALIGO_TEST_MODE", "").lower() in ("1", "y", "true")
                          if test_mode is None else test_mode)
        self.timeout = timeout
        if not (self.api_key and self.user_id):
            raise ProviderError("ALIGO_API_KEY / ALIGO_USER_ID 환경변수가 필요합니다.")
        if not self.sender_key:
            raise ProviderError("ALIGO_SENDER_KEY(카카오 채널 발신프로필 키) 환경변수가 필요합니다.")

    def _token(self) -> str:
        r = requests.post(f"{API_BASE}/token/create/30/s/",
                          data={"apikey": self.api_key, "userid": self.user_id}, timeout=self.timeout)
        data = r.json()
        if str(data.get("code")) != "0":
            raise ProviderError(f"알리고 토큰 발급 실패: {data.get('message')}")
        return data["token"]

    def build_form(self, msg: OutboundMessage, token: str) -> dict:
        if not msg.text:
            raise ProviderError("알리고 발송에는 완성 본문(text)이 필요합니다. rules.yaml 의 text 를 설정하세요.")
        form = {
            "apikey": self.api_key, "userid": self.user_id, "token": token,
            "senderkey": self.sender_key, "tpl_code": msg.template_code, "sender": msg.sender,
            "receiver_1": msg.to,
            "subject_1": (msg.subject or msg.template_code)[:50],
            "message_1": msg.text,
            "failover": "Y" if msg.fallback else "N",
            "testMode": "Y" if self.test_mode else "N",
        }
        if msg.fallback:
            form["fsubject_1"] = (msg.subject or "안내")[:30]
            form["fmessage_1"] = msg.text
        if msg.buttons:
            form["button_1"] = json.dumps({"button": msg.buttons}, ensure_ascii=False)
        return form

    def send(self, msg: OutboundMessage) -> SendResult:
        try:
            token = self._token()
            r = requests.post(f"{API_BASE}/alimtalk/send/", data=self.build_form(msg, token), timeout=self.timeout)
            data = r.json()
        except ProviderError as e:
            return SendResult(ok=False, provider=self.name, error=str(e))
        except (requests.RequestException, ValueError) as e:
            return SendResult(ok=False, provider=self.name, error=f"네트워크/응답 오류: {e}")
        if str(data.get("code")) != "0":
            return SendResult(ok=False, provider=self.name, raw=data, error=f"{data.get('code')} {data.get('message')}")
        info = data.get("info") or {}
        return SendResult(ok=True, provider=self.name, raw=data,
                          message_id=str(info.get("mid")) if info.get("mid") is not None else None)
