"""로그인 세션 캡처 도구.

사용법 (로컬 데스크톱에서, GUI 필요):
    python -m sns.uploaders.login xiaohongshu
    python -m sns.uploaders.login naver

브라우저가 열리면 직접 로그인한 뒤 터미널에서 Enter 를 누르면
sessions/<platform>.json 에 세션이 저장된다. 이후 업로더가 이 세션을 재사용한다.
"""

from __future__ import annotations

import sys

from ..config import load_config
from .base import UploadError
from .browser import session_path

LOGIN_URLS = {
    "xiaohongshu": "https://creator.xiaohongshu.com/login",
    "naver": "https://nid.naver.com/nidlogin.login",
    "naver_blog": "https://nid.naver.com/nidlogin.login",
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in LOGIN_URLS:
        raise SystemExit(f"사용법: python -m sns.uploaders.login <{'|'.join(LOGIN_URLS)}>")
    platform = sys.argv[1]
    if platform == "naver":
        platform = "naver_blog"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise UploadError(
            "playwright 가 설치되지 않았습니다: pip install playwright && playwright install chromium"
        ) from e

    cfg = load_config()
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    state = session_path(cfg.sessions_dir, platform)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR")
        page = context.new_page()
        page.goto(LOGIN_URLS[sys.argv[1]])
        print(f"브라우저에서 {platform} 에 로그인한 뒤, 여기서 Enter 를 누르세요...")
        input()
        context.storage_state(path=str(state))
        browser.close()
    print(f"세션 저장 완료: {state}")


if __name__ == "__main__":
    main()
