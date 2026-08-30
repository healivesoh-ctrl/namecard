"""접수 건 저장소 — JSON 파일 기반(추가 인프라 없이 동작).

DATA_DIR 아래에 claims.json(현재 상태)과 audit.jsonl(추가 전용 감사 로그)을 둔다.
동시 쓰기는 프로세스 내 락 + 원자적 교체(tmp → replace)로 보호한다.
DB로 옮기고 싶으면 이 모듈의 함수 시그니처만 유지하면 된다.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .config import data_dir
from .models import Claim

_lock = threading.RLock()


def _claims_path() -> Path:
    return data_dir() / "claims.json"


def _audit_path() -> Path:
    return data_dir() / "audit.jsonl"


def _read_all() -> dict[str, dict]:
    path = _claims_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_all(rows: dict[str, dict]) -> None:
    path = _claims_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get(claim_id: str) -> Claim | None:
    with _lock:
        row = _read_all().get(claim_id)
        return Claim.from_dict(row) if row else None


def save(claim: Claim) -> Claim:
    with _lock:
        rows = _read_all()
        rows[claim.id] = claim.to_dict()
        _write_all(rows)
        return claim


def list_claims(partner_id: str | None = None, status: str | None = None) -> list[Claim]:
    with _lock:
        claims = [Claim.from_dict(r) for r in _read_all().values()]
    if partner_id:
        claims = [c for c in claims if c.partner_id == partner_id]
    if status:
        claims = [c for c in claims if c.status == status]
    return sorted(claims, key=lambda c: c.created_at, reverse=True)


def count_for_person(partner_id: str, phone_key: str, exclude: str | None = None) -> int:
    """같은 사람(휴대폰 기준)이 이 가게에서 이미 유효하게 받은/받는 중인 건수."""
    from .models import CANCELLED, EXPIRED, PHONE_PENDING, REJECTED

    dead = {REJECTED, CANCELLED, EXPIRED, PHONE_PENDING}
    return sum(
        1
        for c in list_claims(partner_id)
        if c.phone_key == phone_key and c.status not in dead and c.id != exclude
    )


def audit(claim_id: str, event: str, actor: str, detail: dict | None = None) -> None:
    """추가 전용 감사 로그 — 누가·언제·무엇을 승인/변경했는지 남긴다."""
    import time

    line = json.dumps(
        {"at": int(time.time()), "claim_id": claim_id, "event": event,
         "actor": actor, "detail": detail or {}},
        ensure_ascii=False,
    )
    with _lock:
        with _audit_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def audit_trail(claim_id: str) -> list[dict]:
    path = _audit_path()
    if not path.exists():
        return []
    out = []
    with _lock:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("claim_id") == claim_id:
                out.append(row)
    return out
