"""Playwright 공통 헬퍼: 로그인 세션(storage_state) 저장/로드."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .base import UploadError


def session_path(sessions_dir: Path, platform: str) -> Path:
    return sessions_dir / f"{platform}.json"


@contextmanager
def open_page(sessions_dir: Path, platform: str, headless: bool = True):
    """저장된 로그인 세션으로 브라우저 페이지를 연다."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise UploadError(
            "playwright 가 설치되지 않았습니다: pip install playwright && playwright install chromium"
        ) from e

    state = session_path(sessions_dir, platform)
    if not state.exists():
        raise UploadError(
            f"{platform} 로그인 세션이 없습니다. 먼저 실행하세요: "
            f"python -m sns.uploaders.login {platform}"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(state),
            viewport={"width": 1440, "height": 900},
            locale="ko-KR",
        )
        page = context.new_page()
        try:
            yield page
            # 세션 갱신(쿠키 만료 연장)
            context.storage_state(path=str(state))
        finally:
            context.close()
            browser.close()


def save_failure_screenshot(page, draft_dir: Path, platform: str) -> Path | None:
    try:
        path = draft_dir / f"error-{platform}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        return None
