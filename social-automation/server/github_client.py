"""GitHub Contents API 헬퍼 — 서버에서 초안을 리포지토리에 커밋/조회한다.

서버(Render 등)의 파일시스템은 재시작 시 사라지므로, 초안의 원본 저장소는
GitHub 리포지토리다. 대시보드·GitHub Actions·CLI 모두 같은 데이터를 본다.
"""

from __future__ import annotations

import base64

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubRepo:
    def __init__(self, token: str, repo: str, branch: str):
        self.token = token
        self.repo = repo  # "owner/name"
        self.branch = branch

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def list_dir(self, path: str) -> list[dict]:
        r = requests.get(
            f"{API}/repos/{self.repo}/contents/{path}",
            params={"ref": self.branch},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code == 404:
            return []
        if not r.ok:
            raise GitHubError(f"GitHub 목록 조회 실패({r.status_code}): {r.text[:200]}")
        return r.json()

    def get_file(self, path: str) -> tuple[bytes, str] | None:
        """(내용 bytes, blob sha) 반환. 없으면 None."""
        r = requests.get(
            f"{API}/repos/{self.repo}/contents/{path}",
            params={"ref": self.branch},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code == 404:
            return None
        if not r.ok:
            raise GitHubError(f"GitHub 파일 조회 실패({r.status_code}): {r.text[:200]}")
        data = r.json()
        return base64.b64decode(data["content"]), data["sha"]

    def put_file(self, path: str, content: bytes, message: str) -> None:
        """파일 생성/갱신 (기존 파일이면 sha 자동 조회 후 덮어쓰기)."""
        existing = self.get_file(path)
        payload: dict = {
            "message": message,
            "content": base64.b64encode(content).decode(),
            "branch": self.branch,
        }
        if existing:
            payload["sha"] = existing[1]
        r = requests.put(
            f"{API}/repos/{self.repo}/contents/{path}",
            json=payload,
            headers=self._headers(),
            timeout=60,
        )
        if not r.ok:
            raise GitHubError(f"GitHub 커밋 실패({r.status_code}): {r.text[:200]}")
