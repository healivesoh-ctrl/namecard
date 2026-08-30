"""제휴 가게·상품 카탈로그 로딩.

가게와 상품은 수시로 바뀌므로 코드가 아니라 JSON 설정(config/partners.json)에
둔다. 새 가게 추가 = JSON 에 항목 하나 추가. 서버 재시작 없이 /api/admin/reload
로 다시 읽을 수 있다.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def config_path() -> Path:
    env = os.environ.get("PARTNER_CONFIG", "")
    if env:
        return Path(env)
    live = BASE_DIR / "config" / "partners.json"
    return live if live.exists() else BASE_DIR / "config" / "partners.example.json"


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class ConfigError(Exception):
    """설정 파일이 규격에 맞지 않을 때."""


def _validate(cfg: dict) -> None:
    if not isinstance(cfg.get("services"), dict) or not cfg["services"]:
        raise ConfigError("services 가 비어 있습니다.")
    if not isinstance(cfg.get("partners"), list):
        raise ConfigError("partners 는 배열이어야 합니다.")
    ids = set()
    for p in cfg["partners"]:
        pid = p.get("id")
        if not pid or not isinstance(pid, str):
            raise ConfigError("파트너에 id 가 없습니다.")
        if pid in ids:
            raise ConfigError(f"파트너 id 가 중복되었습니다: {pid}")
        ids.add(pid)
        if not p.get("name"):
            raise ConfigError(f"[{pid}] name 이 없습니다.")
        if not p.get("products"):
            raise ConfigError(f"[{pid}] products 가 비어 있습니다.")
        prod_ids = [x.get("id") for x in p["products"]]
        if not all(prod_ids) or len(set(prod_ids)) != len(prod_ids):
            raise ConfigError(f"[{pid}] 상품 id 가 없거나 중복되었습니다.")
        for svc in p.get("services", []):
            if svc not in cfg["services"]:
                raise ConfigError(f"[{pid}] 알 수 없는 서비스: {svc}")


DEFAULT_RULES = {
    "require_store_approval": True,
    "require_daieum_approval": True,
    "transfer_allowed": False,
    "max_claims_per_person": 1,
    "claim_valid_days": 30,
    "pickup_requires_phone_match": True,
    "release_code_period_seconds": 60,
}


def load(force: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is None or force:
            path = config_path()
            if not path.exists():
                raise ConfigError(f"설정 파일이 없습니다: {path}")
            cfg = json.loads(path.read_text(encoding="utf-8"))
            _validate(cfg)
            for p in cfg["partners"]:
                p["rules"] = {**DEFAULT_RULES, **(p.get("rules") or {})}
                p.setdefault("identity_fields", [])
                p.setdefault("services", list(cfg["services"].keys())[:1])
                p.setdefault("active", True)
            _cache = cfg
        return _cache


def partners(active_only: bool = True) -> list[dict]:
    return [p for p in load()["partners"] if p.get("active") or not active_only]


def partner(partner_id: str) -> dict | None:
    return next((p for p in load()["partners"] if p["id"] == partner_id), None)


def service(service_id: str) -> dict | None:
    return load()["services"].get(service_id)


def operator() -> dict:
    return load().get("operator") or {"id": "daieum", "name": "디에이블 다이음"}


def product(partner_id: str, product_id: str) -> dict | None:
    p = partner(partner_id)
    if not p:
        return None
    return next((x for x in p["products"] if x["id"] == product_id), None)


def public_partner(p: dict) -> dict:
    """고객에게 노출해도 되는 필드만 추린 뷰(비밀번호 해시 등 제외)."""
    return {
        "id": p["id"],
        "name": p["name"],
        "active": p.get("active", True),
        "brand": p.get("brand", {}),
        "services": p.get("services", []),
        "identity_fields": p.get("identity_fields", []),
        "products": [
            {k: v for k, v in prod.items() if k != "internal"} for prod in p["products"]
        ],
        "rules": {
            "transfer_allowed": p["rules"]["transfer_allowed"],
            "claim_valid_days": p["rules"]["claim_valid_days"],
            "max_claims_per_person": p["rules"]["max_claims_per_person"],
        },
    }


def partner_password_hash(partner_id: str) -> str | None:
    """가게 관리자 비밀번호 해시. 환경변수가 설정 파일보다 우선."""
    from .security import sha256_hex

    env = os.environ.get(f"PARTNER_ADMIN_PASSWORD__{partner_id.upper().replace('-', '_')}", "")
    if env:
        return sha256_hex(env)
    p = partner(partner_id)
    return (p or {}).get("admin_password_sha256")


def operator_password_hash() -> str | None:
    from .security import sha256_hex

    env = os.environ.get("DAIEUM_ADMIN_PASSWORD", "")
    if env:
        return sha256_hex(env)
    return load().get("operator", {}).get("admin_password_sha256")
