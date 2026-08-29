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


def _draft_text_files(post: Post) -> dict[str, bytes]:
    """플랫폼별 사람이 읽기 쉬운 .txt 파일 내용 생성."""
    files: dict[str, bytes] = {}
    for name, c in post.contents.items():
        lines = ([f"[제목] {c.title}\n"] if c.title else []) + [c.body]
        if c.hashtags:
            lines.append("\n" + c.hashtag_line())
        files[f"{name}.txt"] = "\n".join(lines).encode()
    return files


def _save_draft_files(slug: str, files: dict[str, bytes], message: str) -> str:
    """초안 파일들을 GitHub에 커밋. 불가능하면(토큰 없음·네트워크 제한) 로컬 저장.

    files 의 키는 초안 폴더 기준 상대 파일명. 반환: "github" | "local".
    """
    cfg = _config()
    # 로컬에도 항상 반영(썸네일 서빙·업로더가 로컬 경로를 참조)
    draft_dir = cfg.drafts_dir / slug
    draft_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (draft_dir / name).write_bytes(content)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return "local"
    try:
        gh = GitHubRepo(token, GH_REPO, GH_BRANCH)
        for name, content in files.items():
            gh.put_file(f"{DRAFTS_PATH}/{slug}/{name}", content, message)
        return "github"
    except GitHubError:
        return "local"


def _load_post(slug: str) -> Post:
    """초안 로드: GitHub 우선, 실패 시 로컬."""
    if "/" in slug or ".." in slug:
        raise HTTPException(400, "잘못된 slug 입니다.")
    try:
        gh = GitHubRepo(os.environ.get("GITHUB_TOKEN", ""), GH_REPO, GH_BRANCH)
        found = gh.get_file(f"{DRAFTS_PATH}/{slug}/post.json")
        if found:
            return Post.from_json(found[0].decode())
    except GitHubError:
        pass
    local = _config().drafts_dir / slug / "post.json"
    if local.exists():
        return Post.from_json(local.read_text(encoding="utf-8"))
    raise HTTPException(404, f"초안을 찾을 수 없습니다: {slug}")


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

        if not topic:
            try:
                used = _used_topics(_github())
            except (HTTPException, GitHubError):
                used = {
                    json.loads(p.read_text(encoding="utf-8")).get("topic")
                    for p in cfg.drafts_dir.glob("*/post.json")
                } if cfg.drafts_dir.exists() else set()
            topic = next((t for t in cfg.topics if t not in used), None)
            if not topic:
                raise RuntimeError("주제 큐(config topics)를 모두 사용했습니다. 주제를 직접 입력하세요.")

        JOBS[job_id]["detail"] = f"'{topic}' 콘텐츠 생성 중..."
        post, image_text = generate_post(cfg, topic)

        # 카드 이미지 생성 후 저장(가능하면 GitHub 커밋, 아니면 로컬)
        draft_dir = cfg.drafts_dir / post.slug
        headline = image_text.get("headline") or topic[:15]
        card = make_card(cfg, headline, image_text.get("subline", ""), draft_dir / "card-01.jpg")
        post.images = [card.name]

        JOBS[job_id]["detail"] = "초안 저장 중..."
        files = {"post.json": post.to_json().encode(), "card-01.jpg": card.read_bytes()}
        files.update(_draft_text_files(post))
        storage = _save_draft_files(post.slug, files, f"chore: SNS 콘텐츠 초안 생성 ({post.slug})")

        JOBS[job_id].update(status="done", result=post.slug, storage=storage)
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


# ── 편집: 본문 저장 / 이미지 재생성 / Claude 재작성 ────────
class ContentUpdate(BaseModel):
    title: str = ""
    body: str = ""
    hashtags: list[str] = []


class DraftUpdateBody(BaseModel):
    contents: dict[str, ContentUpdate] = {}
    # platform -> "draft" | "approved" (자동 발행 승인 토글)
    status: dict[str, str] = {}


@app.put("/api/drafts/{slug}")
def update_draft(slug: str, body: DraftUpdateBody,
                 x_admin_password: str | None = Header(default=None)):
    """대시보드에서 편집한 제목/본문/해시태그·승인 상태 저장."""
    _check_password(x_admin_password)
    post = _load_post(slug)
    for platform, upd in body.contents.items():
        if platform not in post.contents:
            raise HTTPException(400, f"알 수 없는 플랫폼: {platform}")
        c = post.contents[platform]
        c.title = upd.title
        c.body = upd.body
        c.hashtags = [t.lstrip("#").strip() for t in upd.hashtags if t.strip()]
    for platform, st in body.status.items():
        if platform not in post.contents:
            raise HTTPException(400, f"알 수 없는 플랫폼: {platform}")
        if st not in ("draft", "approved"):
            raise HTTPException(400, f"상태는 draft/approved 만 가능합니다: {st}")
        post.status[platform] = st
    files = {"post.json": post.to_json().encode()}
    files.update(_draft_text_files(post))
    storage = _save_draft_files(slug, files, f"edit: 콘텐츠 수정 ({slug})")
    return {"message": "저장되었습니다." + (" (로컬 저장)" if storage == "local" else ""),
            "post": json.loads(post.to_json())}


HEX_RE = r"^#[0-9a-fA-F]{6}$"


class ImageBody(BaseModel):
    headline: str
    subline: str = ""
    color_top: str | None = None
    color_bottom: str | None = None


@app.post("/api/drafts/{slug}/image")
def regen_image(slug: str, body: ImageBody,
                x_admin_password: str | None = Header(default=None)):
    """카드 이미지 문구·색상을 바꿔 다시 생성."""
    import re

    from sns.imaging import make_card

    _check_password(x_admin_password)
    for color in (body.color_top, body.color_bottom):
        if color and not re.match(HEX_RE, color):
            raise HTTPException(400, f"색상은 #RRGGBB 형식이어야 합니다: {color}")
    post = _load_post(slug)
    cfg = _config()
    draft_dir = cfg.drafts_dir / slug
    card = make_card(cfg, body.headline, body.subline, draft_dir / "card-01.jpg",
                     color_top=body.color_top, color_bottom=body.color_bottom)
    if "card-01.jpg" not in post.images:
        post.images.insert(0, "card-01.jpg")
    post.image_text = {
        "headline": body.headline,
        "subline": body.subline,
        **({"color_top": body.color_top} if body.color_top else {}),
        **({"color_bottom": body.color_bottom} if body.color_bottom else {}),
    }
    storage = _save_draft_files(
        slug,
        {"post.json": post.to_json().encode(), "card-01.jpg": card.read_bytes()},
        f"edit: 카드 이미지 재생성 ({slug})",
    )
    return {"message": "이미지를 다시 만들었습니다." + (" (로컬 저장)" if storage == "local" else ""),
            "image_text": post.image_text}


class ReviseBody(BaseModel):
    platform: str
    instruction: str


@app.post("/api/drafts/{slug}/revise")
def revise(slug: str, body: ReviseBody,
           x_admin_password: str | None = Header(default=None)):
    """Claude에게 수정 지시를 보내 재작성안을 받는다(저장은 하지 않음 — 검토 후 저장)."""
    _check_password(x_admin_password)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY 가 서버에 설정되지 않았습니다.")
    post = _load_post(slug)
    if body.platform not in post.contents:
        raise HTTPException(400, f"알 수 없는 플랫폼: {body.platform}")

    from sns.generator import revise_content

    c = post.contents[body.platform]
    revised = revise_content(_config(), body.platform, c.title, c.body, c.hashtags,
                             body.instruction)
    return {"revised": revised}


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
    post = _load_post(body.slug)

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
        _save_draft_files(
            body.slug,
            {"post.json": post.to_json().encode()},
            f"chore: 인스타그램 업로드 실패 기록 ({body.slug})",
        )
        raise HTTPException(502, str(e)) from e

    _save_draft_files(
        body.slug,
        {"post.json": post.to_json().encode()},
        f"chore: 인스타그램 업로드 완료 ({body.slug})",
    )
    return {"message": result}
