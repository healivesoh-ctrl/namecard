"""데이터 모델: 생성된 포스트와 플랫폼별 콘텐츠."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class PlatformContent:
    """한 플랫폼에 올라갈 콘텐츠 단위."""

    platform: str
    title: str = ""
    body: str = ""
    hashtags: list[str] = field(default_factory=list)

    def hashtag_line(self, prefix: str = "#") -> str:
        return " ".join(f"{prefix}{t.lstrip('#')}" for t in self.hashtags)


@dataclass
class Post:
    """한 주제로 생성된 멀티플랫폼 포스트 묶음. drafts/<slug>/post.json 으로 저장."""

    topic: str
    slug: str
    created_at: str
    images: list[str] = field(default_factory=list)  # 초안 폴더 기준 상대경로
    contents: dict[str, PlatformContent] = field(default_factory=dict)
    # platform -> "draft" | "uploaded" | "failed:<사유>"
    status: dict[str, str] = field(default_factory=dict)
    # 카드 이미지 문구/색상: {"headline", "subline", "color_top", "color_bottom"}
    image_text: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Post":
        data = json.loads(text)
        contents = {
            k: PlatformContent(**v) for k, v in data.get("contents", {}).items()
        }
        return cls(
            topic=data["topic"],
            slug=data["slug"],
            created_at=data["created_at"],
            images=data.get("images", []),
            contents=contents,
            status=data.get("status", {}),
            image_text=data.get("image_text", {}),
        )

    def save(self, draft_dir: Path) -> Path:
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = draft_dir / "post.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, draft_dir: Path) -> "Post":
        return cls.from_json((draft_dir / "post.json").read_text(encoding="utf-8"))
