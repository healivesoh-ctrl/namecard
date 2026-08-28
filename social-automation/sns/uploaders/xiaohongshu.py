"""샤오홍슈(小红书) 업로드 — 크리에이터 센터 브라우저 자동화.

샤오홍슈는 일반 사업자에게 열린 공식 게시 API가 없어, 본인 계정으로 로그인한
브라우저 세션(Playwright)으로 크리에이터 센터(creator.xiaohongshu.com)의
'이미지+텍스트(图文)' 발행 화면을 자동 조작한다.

주의:
  - 먼저 `python -m sns.uploaders.login xiaohongshu` 로 세션을 저장할 것
  - UI 개편으로 셀렉터가 바뀔 수 있음 → 아래 상수만 수정하면 됨
  - 과도한 자동 게시는 계정 제재 위험이 있으므로 하루 1~2건 수준 권장
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..models import Post
from .base import Uploader, UploadError
from .browser import open_page, save_failure_screenshot

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

# UI 개편 시 여기만 수정
SEL_TAB_IMAGE = "text=上传图文"          # '이미지+텍스트' 탭
SEL_FILE_INPUT = "input[type=file]"
SEL_TITLE = "input[placeholder*='标题']"
SEL_BODY = "div[contenteditable='true']"
SEL_PUBLISH = "button:has-text('发布')"


class XiaohongshuUploader(Uploader):
    name = "xiaohongshu"

    def upload(self, cfg: Config, post: Post, draft_dir: Path, dry_run: bool = False) -> str:
        content = post.contents["xiaohongshu"]
        body_with_tags = content.body.rstrip() + "\n\n" + content.hashtag_line("#")

        if dry_run:
            return (
                f"[dry-run] xiaohongshu: 제목 '{content.title}', "
                f"본문 {len(body_with_tags)}자, 이미지 {len(post.images)}장"
            )

        images = [str((draft_dir / rel).resolve()) for rel in post.images]
        if not images:
            raise UploadError("업로드할 이미지가 없습니다.")

        with open_page(cfg.sessions_dir, self.name) as page:
            try:
                page.goto(PUBLISH_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                if "login" in page.url:
                    raise UploadError("세션이 만료되었습니다. login 도구로 다시 로그인하세요.")

                # '图文' 탭 선택 후 이미지 업로드
                tab = page.locator(SEL_TAB_IMAGE)
                if tab.count():
                    tab.first.click()
                    page.wait_for_timeout(1000)
                page.locator(SEL_FILE_INPUT).first.set_input_files(images)
                page.wait_for_timeout(5000)  # 이미지 처리 대기

                page.locator(SEL_TITLE).first.fill(content.title[:20])
                page.locator(SEL_BODY).first.click()
                page.keyboard.insert_text(body_with_tags)
                page.wait_for_timeout(1000)

                page.locator(SEL_PUBLISH).first.click()
                page.wait_for_timeout(5000)
                return "xiaohongshu 게시 완료"
            except UploadError:
                save_failure_screenshot(page, draft_dir, self.name)
                raise
            except Exception as e:
                shot = save_failure_screenshot(page, draft_dir, self.name)
                raise UploadError(
                    f"xiaohongshu 자동화 실패({e}). UI가 바뀌었을 수 있습니다 — "
                    f"xiaohongshu.py 상단 셀렉터를 확인하세요."
                    + (f" 스크린샷: {shot}" if shot else "")
                ) from e
