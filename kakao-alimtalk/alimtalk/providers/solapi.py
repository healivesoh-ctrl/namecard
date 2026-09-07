"""솔라피(SOLAPI, 구 쿨SMS) 알림톡 발송.

준비: 솔라피 콘솔에서 카카오 비즈니스 채널 연동(pfId 발급) → 알림톡 템플릿 등록·검수 승인
      → API Key/Secret 발급 → 발신번호 등록.
문서: https://developers.solapi.com/references/messages/sendOne
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

import requests

from ..models import OutboundMessage, SendResult
from .base import ProviderError

API_BASE = "https://api.solapi.com"


class SolapiProvider:
    name = "solapi"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 pf_id: str | None = None, timeout: float = 15):
        self.api_key = api_key or os.environ.get("SOLAPI_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("SOLAPI_API_SECRET", "")
        self.pf_id = pf_id or os.environ.get("SOLAPI_PF_ID") or os.environ.get("KAKAO_CHANNEL_ID", "")
        self.timeout = timeout
        if not (self.api_key and self.api_secret):
            raise ProviderError("SOLAPI_API_KEY / SOLAPI_API_SECRET 환경변수가 필요합니다.")
        if not self.pf_id:
            raise ProviderError("SOLAPI_PF_ID(카카오 채널 pfId) 환경변수가 필요합니다.")

    def _auth_header(self) -> str:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        salt = secrets.token_hex(16)
        sig = hmac.new(self.api_secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
        return f"HMAC-SHA256 apiKey={self.api_key}, date={date}, salt={salt}, signature={sig}"

    def build_payload(self, msg: OutboundMessage) -> dict:
        variables = {(k if k.startswith("#{") else f"#{{{k}}}"): v for k, v in msg.variables.items()}
        kakao: dict = {"pfId": self.pf_id, "templateId": msg.template_code,
                       "variables": variables, "disableSms": not msg.fallback}
        message: dict = {"to": msg.to, "from": msg.sender, "type": "ATA", "kakaoOptions": kakao}
        if msg.text:
            message["text"] = msg.text        # 알림톡 실패 시 SMS/LMS 대체 발송 본문
        if msg.subject:
            message["subject"] = msg.subject
        return {"message": message}

    def send(self, msg: OutboundMessage) -> SendResult:
        try:
            r = requests.post(f"{API_BASE}/messages/v4/send", json=self.build_payload(msg),
                              headers={"Authorization": self._auth_header(), "Content-Type": "application/json"},
                              timeout=self.timeout)
        except requests.RequestException as e:
            return SendResult(ok=False, provider=self.name, error=f"네트워크 오류: {e}")
        try:
            data = r.json()
        except ValueError:
            data = {"body": r.text[:500]}
        if r.status_code != 200:
            return SendResult(ok=False, provider=self.name, raw=data,
                              error=f"HTTP {r.status_code}: {data.get('errorMessage') or data.get('errorCode') or data}")
        code = str(data.get("statusCode", ""))
        if code.startswith("4"):
            return SendResult(ok=False, provider=self.name, raw=data, message_id=data.get("messageId"),
                              error=f"{code} {data.get('statusMessage', '')}")
        return SendResult(ok=True, provider=self.name, raw=data, message_id=data.get("messageId"))
