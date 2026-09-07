from __future__ import annotations

from typing import Protocol

from ..models import OutboundMessage, SendResult


class Provider(Protocol):
    name: str

    def send(self, msg: OutboundMessage) -> SendResult: ...


class ProviderError(Exception):
    pass
