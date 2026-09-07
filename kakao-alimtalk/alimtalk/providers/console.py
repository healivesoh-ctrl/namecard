"""실제 발송 없이 로그만 남기는 발송사 (개발·리허설용)."""

from __future__ import annotations

import logging
import uuid

from ..models import OutboundMessage, SendResult
from ..phone import mask_phone

log = logging.getLogger("alimtalk.console")


class ConsoleProvider:
    name = "console"

    def send(self, msg: OutboundMessage) -> SendResult:
        log.info("[DRY-RUN] → %s 템플릿=%s 변수=%s\n%s",
                 mask_phone(msg.to), msg.template_code, msg.variables, msg.text or "")
        print(f"[DRY-RUN] {mask_phone(msg.to)} | {msg.template_code} | {msg.variables}"
              + (f"\n{msg.text}" if msg.text else ""))
        return SendResult(ok=True, provider=self.name, channel="dry-run", message_id=f"dry-{uuid.uuid4().hex[:8]}")
