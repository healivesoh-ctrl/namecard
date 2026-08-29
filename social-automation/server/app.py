"""SNS 자동화 웹 서비스.

대시보드(정적 HTML)를 서빙하고, 콘텐츠 생성·인스타그램 업로드 API를 제공한다.
초안의 원본 저장소는 GitHub 리포지토리(Contents API로 커밋)이므로 서버가
재시작돼도 데이터가 유지되고, 대시보드·Actions·CLI와 데이터가 일치한다.

실행 (social-automation/ 에서):
    uvicorn server.app:app --host 0.0.0.0 --port 8000

필수 환경변수:
    ANTHROPIC_API_KEY  콘텐츠 생성
    GITHUB_TOKEN       초안 커밋용 (repo 쓰기 권한, fine-grained: Contents RW)
    ADMIN_PASSWORD     생성/업로드 API 보호 비밀번호
선택:
    GH_REPO   (기본 healivesoh-ctrl/namecard)
    GH_BRANCH (기본 main)
    IG_ACCESS_TOKEN / IG_USER_ID  인스타그램 업로드
    IMAGE_BASE_URL (기본: raw.githubusercontent.com 자동 구성)
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sns.config import BASE_DIR, Config, load_config  # noqa: E402
from sns.models import Post  # noqa: E402

from .github_client import GitHubError, GitHubRepo  # noqa: E402

GH_REPO = os.environ.get("GH_REPO", "healivesoh-ctrl/namecard")
GH_BRANCH = os.environ.get("GH_BRANCH", "main")
DRAFTS_PATH = "social-automation/drafts"

app = FastAPI(title="SNS 자동화", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # GET은 공개 데이터, 변경 API는 비밀번호로 보호
    allow_methods=["*"],
    allow_headers=["*"],
)

# 진행 중인 생성 작업 (in-memory; 서버 1대 전제)
JOBS: dict[str, dict] = {}


@app.exception_handler(GitHubError)
async def _github_error_handler(request, exc: GitHubError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=502, content={"detail": str(exc)})


def _config() -> Config:
    path = BASE_DIR / "config.yaml"
    if not path.exists():
        path = BASE_DIR / "config.example.yaml"
    cfg = load_config(path)
    if not os.environ.get("IMAGE_BASE_URL"):
        os.environ["IMAGE_BASE_URL"] = (
            f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/social-automation"
        )
    return cfg


def _github() -> GitHubRepo:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise HTTPException(500, "GITHUB_TOKEN 이 서버에 설정되지 않았습니다.")
    return GitHubRepo(token, GH_REPO, GH_BRANCH)


def _check_password(x_admin_password: str | None) -> None:
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(403, "서버에 ADMIN_PASSWORD 가 설정되지 않아 변경 작업이 비활성화되어 있습니다.")
    if not x_admin_password or not hmac.compare_digest(x_admin_password, expected):
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")


# ── 정적: 대시보드 ─────────────────────────────────────────
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


@app.get("/")
def index():
    return FileResponse(DASHBOARD, media_type="text/html")


@app.get("/api/health")
def health():
    return {"ok": True, "repo": GH_REPO, "branch": GH_BRANCH}


@app.get("/api/drafts")
def list_drafts():
    """초안 목록(공개 데이터). 대시보드가 서버 연결 시 GitHub 대신 이 API를 사용한다.

    GitHub에 접근할 수 없으면(토큰 없음·네트워크 제한 등) 서버 로컬 디스크의
    초안으로 폴백한다(로컬 개발 모드).
    """
    drafts = []
    try:
        # 읽기는 public repo 라면 토큰 없이도 가능
        gh = GitHubRepo(os.environ.get("GITHUB_TOKEN", ""), GH_REPO, GH_BRANCH)
        for item in gh.list_dir(DRAFTS_PATH):
            if item.get("type") != "dir":
                continue
            f = gh.get_file(f"{DRAFTS_PATH}/{item['name']}/post.json")
            if not f:
                continue
            try:
                drafts.append(json.loads(f[0]))
            except json.JSONDecodeError:
                continue
    except GitHubError:
        cfg = _config()
        if cfg.drafts_dir.exists():
            for d in cfg.drafts_dir.iterdir():
                pj = d / "post.json"
                if pj.exists():
                    try:
                        drafts.append(json.loads(pj.read_text(encoding="utf-8")))
                    except json.JSONDecodeError:
                        continue
    drafts.sort(key=lambda d: d.get("slug", ""), reverse=True)
    return {"branch": GH_BRANCH, "drafts": drafts}


@app.get("/api/file/{slug}/{filename}")
def get_draft_file(slug: str, filename: str):
    """초안 폴더의 파일(카드 이미지 등)을 서빙 — 대시보드 썸네일용."""
    import mimetypes

    from fastapi.responses import Response

    if "/" in slug or ".." in slug or "/" in filename or ".." in filename:
        raise HTTPException(400, "잘못된 경로입니다.")
    content: bytes | None = None
    try:
        gh = GitHubRepo(os.environ.get("GITHUB_TOKEN", ""), GH_REPO, GH_BRANCH)
        found = gh.get_file(f"{DRAFTS_PATH}/{slug}/{filename}")
        if found:
            content = found[0]
    except GitHubError:
        pass
    if content is None:
        local = _config().drafts_dir / slug / filename
        if local.exists():
            content = local.read_bytes()
    if content is None:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=3600"})


# ── 생성 ──────────────────────────────────────────────────
class GenerateBody(BaseModel):
    topic: str | None = None


def _used_topics(gh: GitHubRepo) -> set[str]:
    used = set()
    for item in gh.list_dir(DRAFTS_PATH):
        if item.get("type") != "dir":
            continue
        f = gh.get_file(f"{DRAFTS_PATH}/{item['name']}/post.json")
        if f:
            try:
                used.add(json.loads(f[0])["topic"])
            except (json.JSONDecodeError, KeyError):
                pass
    return used


def _run_generate(job_id: str, topic: str | None) -> None:
    try:
        from sns.generator import generate_post
        from sns.imaging import make_card

        cfg = _config()
        gh = _github()

        if not topic:
            used = _used_topics(gh)
            topic = next((t for t in cfg.topics if t not in used), None)
            if not topic:
                raise RuntimeError("주제 큐(config topics)를 모두 사용했습니다. 주제를 직접 입력하세요.")

        JOBS[job_id]["detail"] = f"'{topic}' 콘텐츠 생성 중..."
        post, image_text = generate_post(cfg, topic)

        # 서버 로컬(임시)에 카드 이미지 생성 후 GitHub에 커밋
        draft_dir = cfg.drafts_dir / post.slug
        headline = image_text.get("headline") or topic[:15]
        card = make_card(cfg, headline, image_text.get("subline", ""), draft_dir / "card-01.jpg")
        post.images = [card.name]

        JOBS[job_id]["detail"] = "GitHub에 초안 커밋 중..."
        base = f"{DRAFTS_PATH}/{post.slug}"
        msg = f"chore: SNS 콘텐츠 초안 생성 ({post.slug})"
        gh.put_file(f"{base}/post.json", post.to_json().encode(), msg)
        gh.put_file(f"{base}/card-01.jpg", card.read_bytes(), msg)
        for name, c in post.contents.items():
            lines = ([f"[제목] {c.title}\n"] if c.title else []) + [c.body]
            if c.hashtags:
                lines.append("\n" + c.hashtag_line())
            gh.put_file(f"{base}/{name}.txt", "\n".join(lines).encode(), msg)

        JOBS[job_id].update(status="done", result=post.slug)
    except Exception as e:  # 작업 스레드이므로 모든 예외를 잡아 상태로 기록
        JOBS[job_id].update(status="failed", error=str(e))


@app.post("/api/generate")
def generate(body: GenerateBody, x_admin_password: str | None = Header(default=None)):
    _check_password(x_admin_password)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY 가 서버에 설정되지 않았습니다.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "detail": "시작..."}
    threading.Thread(target=_run_generate, args=(job_id, body.topic), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job


# ── 업로드 (인스타그램) ────────────────────────────────────
class UploadBody(BaseModel):
    slug: str
    platform: str = "instagram"


@app.post("/api/upload")
def upload(body: UploadBody, x_admin_password: str | None = Header(default=None)):
    _check_password(x_admin_password)
    if body.platform != "instagram":
        raise HTTPException(
            400,
            "서버에서는 인스타그램만 업로드할 수 있습니다. 샤오홍슈·네이버는 로그인 세션이 "
            "필요해 로컬 CLI 또는 대시보드의 복사 버튼으로 게시하세요.",
        )

    from sns.uploaders import UploadError, get_uploader

    cfg = _config()
    gh = _github()
    found = gh.get_file(f"{DRAFTS_PATH}/{body.slug}/post.json")
    if not found:
        raise HTTPException(404, f"초안을 찾을 수 없습니다: {body.slug}")
    post = Post.from_json(found[0].decode())

    # 업로더는 이미지의 '리포 내 상대경로'로 공개 URL을 구성하므로,
    # 로컬에 같은 경로 구조만 만들어 주면 된다(파일 내용은 불필요).
    draft_dir = cfg.drafts_dir / body.slug
    draft_dir.mkdir(parents=True, exist_ok=True)
    for rel in post.images:
        p = draft_dir / rel
        if not p.exists():
            p.touch()

    try:
        result = get_uploader("instagram").upload(cfg, post, draft_dir)
        post.status["instagram"] = "uploaded"
    except UploadError as e:
        post.status["instagram"] = f"failed:{e}"
        gh.put_file(
            f"{DRAFTS_PATH}/{body.slug}/post.json",
            post.to_json().encode(),
            f"chore: 인스타그램 업로드 실패 기록 ({body.slug})",
        )
        raise HTTPException(502, str(e)) from e

    gh.put_file(
        f"{DRAFTS_PATH}/{body.slug}/post.json",
        post.to_json().encode(),
        f"chore: 인스타그램 업로드 완료 ({body.slug})",
    )
    return {"message": result}
