"""네이버 블로그 업로드 — 스마트에디터 ONE 브라우저 자동화.

네이버는 블로그 글쓰기 공식 API를 종료했기 때문에, 로그인 세션(Playwright)으로
글쓰기 화면(blog.naver.com/GoBlogWrite.naver)을 자동 조작한다.

주의:
  - 먼저 `python -m sns.uploaders.login naver` 로 세션을 저장할 것
    (자동입력방지 때문에 로그인은 반드시 사람이 직접)
  - 에디터가 iframe(mainFrame) 안에 있음
  - UI 개편 시 아래 상수만 수정
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..models import Post
from .base import Uploader, UploadError
from .browser import open_page, save_failure_screenshot

WRITE_URL = "https://blog.naver.com/GoBlogWrite.naver"

SEL_IFRAME = "iframe#mainFrame"
SEL_HELP_CLOSE = "button.se-help-panel-close-button, .se-popup-button-cancel"
SEL_TITLE = ".se-title-text"
SEL_BODY = ".se-component-content p"
SEL_PUBLISH_OPEN = "button[data-click-area='tpb.publish'], .publish_btn__m9KHH"
SEL_PUBLISH_CONFIRM = "button[data-testid='seOnePublishBtn'], .confirm_btn__WEaBq"
SEL_TAG_INPUT = "input#tag-input, input[placeholder*='태그']"


def markdown_to_plain(md: str) -> str:
    """생성된 마크다운을 에디터에 붙일 평문으로 변환(제목 기호 제거)."""
    text = re.sub(r"^#{1,6}\s*", "", md, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    return text


class NaverBlogUploader(Uploader):
    name = "naver_blog"

    def upload(self, cfg: Config, post: Post, draft_dir: Path, dry_run: bool = False) -> str:
        content = post.contents["naver_blog"]
        body = markdown_to_plain(content.body)

        if dry_run:
            return (
                f"[dry-run] naver_blog: 제목 '{content.title}', 본문 {len(body)}자, "
                f"태그 {len(content.hashtags)}개"
            )

        with open_page(cfg.sessions_dir, self.name) as page:
            try:
                page.goto(WRITE_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                if "nidlogin" in page.url:
                    raise UploadError("세션이 만료되었습니다. login 도구로 다시 로그인하세요.")

                frame = page.frame_locator(SEL_IFRAME)

                # 도움말/이어쓰기 팝업 닫기(있을 때만)
                close_btn = frame.locator(SEL_HELP_CLOSE)
                if close_btn.count():
                    try:
                        close_btn.first.click(timeout=3000)
                    except Exception:
                        pass

                # 제목
                frame.locator(SEL_TITLE).first.click()
                page.keyboard.insert_text(content.title)

                # 본문 (문단 단위 입력)
                frame.locator(SEL_BODY).first.click()
                for i, para in enumerate(body.split("\n")):
                    if i:
                        page.keyboard.press("Enter")
                    if para.strip():
                        page.keyboard.insert_text(para)

                # 이미지 첨부는 스마트에디터의 사진 업로드 UI가 OS 파일창을 띄워
                # 자동화가 불안정하므로 기본은 텍스트만 게시한다.

                # 발행 패널 열기 → 태그 입력 → 확인 (모두 iframe 내부)
                frame.locator(SEL_PUBLISH_OPEN).first.click()
                page.wait_for_timeout(1500)

                tag_input = frame.locator(SEL_TAG_INPUT)
                if tag_input.count():
                    for tag in content.hashtags[:10]:
                        tag_input.first.fill(tag.lstrip("#"))
                        page.keyboard.press("Enter")

                frame.locator(SEL_PUBLISH_CONFIRM).first.click()
                page.wait_for_timeout(5000)
                return f"naver_blog 게시 완료 (현재 URL: {page.url})"
            except UploadError:
                save_failure_screenshot(page, draft_dir, self.name)
                raise
            except Exception as e:
                shot = save_failure_screenshot(page, draft_dir, self.name)
                raise UploadError(
                    f"naver_blog 자동화 실패({e}). UI가 바뀌었을 수 있습니다 — "
                    f"naver_blog.py 상단 셀렉터를 확인하세요."
                    + (f" 스크린샷: {shot}" if shot else "")
                ) from e
