"""서명 토큰·전화번호 해시·1회용 인증코드 등 보안 유틸.

외부 의존성 없이 표준 라이브러리(hmac/hashlib/secrets)만 사용한다.
모든 서명은 SERVER_SECRET 환경변수를 키로 하는 HMAC-SHA256 이다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

_DEV_SECRET_FILE = ".dev-secret"


def server_secret() -> bytes:
    """서명 키. 운영에서는 SERVER_SECRET 필수(없으면 개발용 키를 파일에 고정 생성)."""
    env = os.environ.get("SERVER_SECRET", "")
    if env:
        return env.encode()
    from .config import data_dir

    path = data_dir() / _DEV_SECRET_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip().encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: bytes) -> str:
    return _b64e(hmac.new(server_secret(), payload, hashlib.sha256).digest())


def issue_token(data: dict, ttl_seconds: int) -> str:
    """만료 시각을 담은 서명 토큰 발급. 형식: base64(payload).base64(sig)"""
    body = dict(data, exp=int(time.time()) + ttl_seconds)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64e(raw)}.{sign(raw)}"


def read_token(token: str | None) -> dict | None:
    """토큰 검증. 위조·만료면 None."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    try:
        raw = _b64d(body)
    except Exception:
        return None
    if not hmac.compare_digest(sig, sign(raw)):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data


# ── 전화번호 ──────────────────────────────────────────────
def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    return digits


def valid_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"01[016789][0-9]{7,8}", normalize_phone(phone)))


def phone_key(phone: str) -> str:
    """전화번호를 그대로 저장하지 않기 위한 결정적 해시(중복·양도 판별 키)."""
    return hmac.new(server_secret(), normalize_phone(phone).encode(), hashlib.sha256).hexdigest()


def mask_phone(phone: str) -> str:
    d = normalize_phone(phone)
    return f"{d[:3]}-****-{d[-4:]}" if len(d) >= 10 else "***"


def mask_name(name: str) -> str:
    name = (name or "").strip()
    if len(name) <= 1:
        return name or "*"
    return name[0] + "*" * (len(name) - 2) + name[-1] if len(name) > 2 else name[0] + "*"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── 1회용 인증코드(휴대폰 본인확인) ────────────────────────
def new_otp() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def otp_digest(claim_id: str, code: str) -> str:
    return hmac.new(server_secret(), f"{claim_id}:{code}".encode(), hashlib.sha256).hexdigest()


# ── 회전 수령코드(양방향 승인 완료 후에만 생성) ────────────
def release_code(claim_id: str, nonce: str, period: int = 60, at: float | None = None,
                 offset: int = 0) -> str:
    """60초마다 바뀌는 6자리 코드.

    캡처해서 지인에게 넘겨도 곧 무효가 되므로 '양도' 시도를 실질적으로 막는다.
    nonce 는 양방향 승인이 모두 끝난 시점에만 발급되므로, 한쪽 승인만으로는
    코드 자체가 존재할 수 없다.
    """
    step = int((at if at is not None else time.time()) // period) + offset
    msg = f"{claim_id}:{nonce}:{step}".encode()
    digest = hmac.new(server_secret(), msg, hashlib.sha256).digest()
    return f"{int.from_bytes(digest[-4:], 'big') % 1000000:06d}"


def release_code_valid(claim_id: str, nonce: str, code: str, period: int = 60) -> bool:
    """직전 스텝까지 허용(입력 지연 대비)."""
    code = re.sub(r"\D", "", code or "")
    return any(
        hmac.compare_digest(release_code(claim_id, nonce, period, offset=off), code)
        for off in (0, -1)
    )


def seconds_left(period: int = 60) -> int:
    return period - int(time.time()) % period
