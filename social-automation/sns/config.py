"""config.yaml + .env 로드."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 미설치 환경(CI 등)에서도 동작
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

# social-automation/ 디렉터리
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    # --- 브랜드/생성 설정 -------------------------------------------------
    @property
    def brand(self) -> dict[str, Any]:
        return self.raw.get("brand", {})

    @property
    def model(self) -> str:
        return self.raw.get("model", "claude-opus-5")

    @property
    def topics(self) -> list[str]:
        return list(self.raw.get("topics", []))

    def platform(self, name: str) -> dict[str, Any]:
        return self.raw.get("platforms", {}).get(name, {})

    def enabled_platforms(self) -> list[str]:
        return [
            name
            for name, cfg in self.raw.get("platforms", {}).items()
            if cfg.get("enabled", False)
        ]

    @property
    def image(self) -> dict[str, Any]:
        return self.raw.get("image", {})

    # --- 경로 -------------------------------------------------------------
    @property
    def drafts_dir(self) -> Path:
        return BASE_DIR / self.raw.get("paths", {}).get("drafts", "drafts")

    @property
    def sessions_dir(self) -> Path:
        return BASE_DIR / self.raw.get("paths", {}).get("sessions", "sessions")

    # --- 비밀값(.env) -----------------------------------------------------
    @property
    def ig_access_token(self) -> str:
        return os.environ.get("IG_ACCESS_TOKEN", "")

    @property
    def ig_user_id(self) -> str:
        return os.environ.get("IG_USER_ID", "")

    @property
    def image_base_url(self) -> str:
        return os.environ.get("IMAGE_BASE_URL", "").rstrip("/")


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(BASE_DIR / ".env")
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    if not cfg_path.exists():
        example = BASE_DIR / "config.example.yaml"
        if example.exists():
            raise SystemExit(
                f"설정 파일이 없습니다: {cfg_path}\n"
                f"먼저 복사해서 만드세요: cp {example} {cfg_path}"
            )
        raise SystemExit(f"설정 파일이 없습니다: {cfg_path}")
    with open(cfg_path, encoding="utf-8") as f:
        return Config(raw=yaml.safe_load(f) or {})
