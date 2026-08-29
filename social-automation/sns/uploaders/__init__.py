"""플랫폼별 업로더."""

from __future__ import annotations

from .base import UploadError, Uploader


def get_uploader(name: str):
    if name == "instagram":
        from .instagram import InstagramUploader

        return InstagramUploader()
    if name == "xiaohongshu":
        from .xiaohongshu import XiaohongshuUploader

        return XiaohongshuUploader()
    if name == "naver_blog":
        from .naver_blog import NaverBlogUploader

        return NaverBlogUploader()
    raise ValueError(f"알 수 없는 플랫폼: {name}")


__all__ = ["Uploader", "UploadError", "get_uploader"]
