"""한국 휴대폰 번호 정규화.

설문폼에서 들어오는 다양한 표기(010-1234-5678, 010 1234 5678, +82 10-1234-5678,
82-10-1234-5678, 1012345678(엑셀이 앞의 0을 지운 경우))를 모두
'01012345678' 형태의 숫자 문자열로 통일한다. 휴대폰 번호가 아니면 None.
"""

from __future__ import annotations

import re

MOBILE_PREFIXES = ("010", "011", "016", "017", "018", "019")


def normalize_phone(raw: object) -> str | None:
    """휴대폰 번호를 숫자만 남긴 표준형('01012345678')으로 변환. 실패 시 None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # 여러 번호가 적힌 경우 첫 번째만 사용 ("010-1111-2222 / 010-3333-4444")
    s = re.split(r"[/,;]| 또는 |\s{2,}", s)[0]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    # 국가번호 처리: 0082..., 82...
    if digits.startswith("0082"):
        digits = "0" + digits[4:]
    elif digits.startswith("82") and len(digits) >= 11:
        digits = "0" + digits[2:]
    # 엑셀/스프레드시트가 앞자리 0 을 지운 경우 (1012345678)
    if len(digits) == 10 and digits[:2] in {p[1:] for p in MOBILE_PREFIXES}:
        digits = "0" + digits
    if digits.startswith(MOBILE_PREFIXES) and len(digits) in (10, 11):
        return digits
    return None


def format_phone(digits: str) -> str:
    """'01012345678' → '010-1234-5678' (표시용)."""
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def mask_phone(digits: str) -> str:
    """'01012345678' → '010-****-5678' (로그·대시보드 표시용)."""
    if len(digits) >= 8:
        return f"{digits[:3]}-****-{digits[-4:]}"
    return "***"
