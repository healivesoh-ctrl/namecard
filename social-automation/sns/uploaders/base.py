"""업로더 공통 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Config
from ..models import Post


class UploadError(RuntimeError):
    pass


class Uploader(ABC):
    name: str = ""

    @abstractmethod
    def upload(self, cfg: Config, post: Post, draft_dir: Path, dry_run: bool = False) -> str:
        """업로드를 수행하고 결과 설명 문자열(게시물 ID/URL 등)을 반환한다.

        dry_run=True 이면 실제 업로드 없이 수행 내용만 출력한다.
        실패 시 UploadError 를 던진다.
        """
