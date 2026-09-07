"""발송사 선택. ALIMTALK_DRY_RUN=1 이면 어떤 설정이든 콘솔(실발송 없음)로 동작."""

from __future__ import annotations

import os

from .base import Provider, ProviderError
from .console import ConsoleProvider

PROVIDERS = ("console", "solapi", "aligo")


def get_provider(name: str | None = None) -> Provider:
    if os.environ.get("ALIMTALK_DRY_RUN", "").lower() in ("1", "true", "y"):
        return ConsoleProvider()
    name = (name or os.environ.get("ALIMTALK_PROVIDER") or "console").lower()
    if name == "console":
        return ConsoleProvider()
    if name == "solapi":
        from .solapi import SolapiProvider
        return SolapiProvider()
    if name == "aligo":
        from .aligo import AligoProvider
        return AligoProvider()
    raise ProviderError(f"알 수 없는 발송사: {name} (가능: {', '.join(PROVIDERS)})")


__all__ = ["Provider", "ProviderError", "ConsoleProvider", "get_provider", "PROVIDERS"]
