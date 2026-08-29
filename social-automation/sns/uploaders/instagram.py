"""Instagram 공식 Graph API 업로드.

필요 조건:
  1. Instagram 프로페셔널(비즈니스/크리에이터) 계정 + Facebook 페이지 연결
  2. Meta 개발자 앱에서 instagram_basic, instagram_content_publish,
     pages_read_engagement 권한을 가진 장기 액세스 토큰 발급
  3. .env 에 IG_ACCESS_TOKEN, IG_USER_ID 설정
  4. 이미지는 공개 URL이어야 함 → drafts/ 를 push한 뒤 IMAGE_BASE_URL 로 접근
     (예: https://raw.githubusercontent.com/<owner>/<repo>/<branch>/social-automation)

동작: 컨테이너 생성(/media) → 게시(/media_publish). 이미지 2장 이상이면 캐러셀.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from ..config import BASE_DIR, Config
from ..models import Post
from .base import Uploader, UploadError

GRAPH = "https://graph.facebook.com/v21.0"


class InstagramUploader(Uploader):
    name = "instagram"

    def _api(self, method: str, path: str, token: str, **params) -> dict:
        params["access_token"] = token
        r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise UploadError(f"Graph API 오류: {err.get('message')} (code {err.get('code')})")
        return data

    def _image_urls(self, cfg: Config, post: Post, draft_dir: Path) -> list[str]:
        base = cfg.image_base_url
        if not base:
            raise UploadError(
                "IMAGE_BASE_URL 이 설정되지 않았습니다. Instagram API는 공개 이미지 URL이 "
                "필요합니다(.env 참고)."
            )
        urls = []
        for rel in post.images:
            rel_from_base = (draft_dir / rel).resolve().relative_to(BASE_DIR)
            urls.append(f"{base}/{rel_from_base.as_posix()}")
        if not urls:
            raise UploadError("업로드할 이미지가 없습니다.")
        return urls

    def _wait_ready(self, container_id: str, token: str, timeout_s: int = 120) -> None:
        """컨테이너가 FINISHED 될 때까지 폴링(이미지 다운로드/검증에 시간이 걸림)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data = self._api("GET", container_id, token, fields="status_code")
            code = data.get("status_code")
            if code == "FINISHED":
                return
            if code == "ERROR":
                raise UploadError("미디어 컨테이너 처리 실패(이미지 URL 접근 가능 여부 확인)")
            time.sleep(3)
        raise UploadError("미디어 컨테이너 준비 시간 초과")

    def upload(self, cfg: Config, post: Post, draft_dir: Path, dry_run: bool = False) -> str:
        token, user_id = cfg.ig_access_token, cfg.ig_user_id
        if not dry_run and (not token or not user_id):
            raise UploadError("IG_ACCESS_TOKEN / IG_USER_ID 가 설정되지 않았습니다(.env 참고).")

        content = post.contents["instagram"]
        caption = content.body.rstrip() + "\n\n" + content.hashtag_line()

        if dry_run:
            return f"[dry-run] instagram: 이미지 {len(post.images)}장, 캡션 {len(caption)}자"

        urls = self._image_urls(cfg, post, draft_dir)

        if len(urls) == 1:
            container = self._api(
                "POST", f"{user_id}/media", token, image_url=urls[0], caption=caption
            )["id"]
        else:  # 캐러셀
            children = []
            for url in urls[:10]:
                child = self._api(
                    "POST", f"{user_id}/media", token, image_url=url, is_carousel_item="true"
                )["id"]
                self._wait_ready(child, token)
                children.append(child)
            container = self._api(
                "POST",
                f"{user_id}/media",
                token,
                media_type="CAROUSEL",
                children=",".join(children),
                caption=caption,
            )["id"]

        self._wait_ready(container, token)
        media_id = self._api("POST", f"{user_id}/media_publish", token, creation_id=container)["id"]
        return f"instagram 게시 완료 (media_id={media_id})"
