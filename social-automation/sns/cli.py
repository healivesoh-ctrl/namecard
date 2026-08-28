"""SNS 자동화 CLI.

사용 예 (social-automation/ 디렉터리에서):
    python -m sns.cli generate --topic "NFC 명함이란?"   # 주제 지정 생성
    python -m sns.cli generate --auto                    # config topics 큐에서 자동 선택
    python -m sns.cli list                               # 초안 목록
    python -m sns.cli preview 2026-08-28-nfc-명함이란    # 초안 미리보기
    python -m sns.cli upload 2026-08-28-nfc-명함이란 --platforms instagram --dry-run
    python -m sns.cli run --auto --platforms instagram   # 생성+업로드 한 번에(cron용)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, load_config
from .models import Post


def _used_topics(cfg: Config) -> set[str]:
    used = set()
    if cfg.drafts_dir.exists():
        for d in cfg.drafts_dir.iterdir():
            pj = d / "post.json"
            if pj.exists():
                try:
                    used.add(json.loads(pj.read_text(encoding="utf-8"))["topic"])
                except (json.JSONDecodeError, KeyError):
                    pass
    return used


def _pick_auto_topic(cfg: Config) -> str:
    used = _used_topics(cfg)
    for topic in cfg.topics:
        if topic not in used:
            return topic
    raise SystemExit(
        "config.yaml 의 topics 를 모두 사용했습니다. 새 주제를 추가하거나 --topic 으로 지정하세요."
    )


def cmd_generate(cfg: Config, args: argparse.Namespace) -> str:
    from .generator import generate_post
    from .imaging import make_card

    topic = args.topic or _pick_auto_topic(cfg)
    print(f"주제: {topic}")
    print(f"모델 {cfg.model} 로 콘텐츠 생성 중...")
    post, image_text = generate_post(cfg, topic)

    draft_dir = cfg.drafts_dir / post.slug
    headline = image_text.get("headline") or topic[:15]
    subline = image_text.get("subline", "")
    card = make_card(cfg, headline, subline, draft_dir / "card-01.jpg")
    post.images = [card.name]
    post.save(draft_dir)

    # 사람이 읽기 쉬운 플랫폼별 텍스트 파일도 같이 저장
    for name, c in post.contents.items():
        lines = []
        if c.title:
            lines.append(f"[제목] {c.title}\n")
        lines.append(c.body)
        if c.hashtags:
            lines.append("\n" + c.hashtag_line())
        (draft_dir / f"{name}.txt").write_text("\n".join(lines), encoding="utf-8")

    print(f"초안 저장 완료: {draft_dir}")
    return post.slug


def cmd_list(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.drafts_dir.exists():
        print("초안이 없습니다.")
        return
    for d in sorted(cfg.drafts_dir.iterdir()):
        if (d / "post.json").exists():
            post = Post.load(d)
            status = ", ".join(f"{k}:{v}" for k, v in post.status.items())
            print(f"{post.slug}  [{status}]  {post.topic}")


def cmd_preview(cfg: Config, args: argparse.Namespace) -> None:
    draft_dir = cfg.drafts_dir / args.slug
    if not (draft_dir / "post.json").exists():
        raise SystemExit(f"초안을 찾을 수 없습니다: {args.slug}")
    post = Post.load(draft_dir)
    print(f"주제: {post.topic}\n생성: {post.created_at}\n이미지: {post.images}\n")
    for name, c in post.contents.items():
        print(f"── {name} " + "─" * 40)
        if c.title:
            print(f"[제목] {c.title}")
        print(c.body[:800] + ("..." if len(c.body) > 800 else ""))
        print(f"[태그] {c.hashtag_line()}\n")


def cmd_upload(cfg: Config, args: argparse.Namespace, slug: str | None = None) -> int:
    from .uploaders import UploadError, get_uploader

    slug = slug or args.slug
    draft_dir = cfg.drafts_dir / slug
    if not (draft_dir / "post.json").exists():
        raise SystemExit(f"초안을 찾을 수 없습니다: {slug}")
    post = Post.load(draft_dir)

    platforms = (
        [p.strip() for p in args.platforms.split(",")]
        if args.platforms
        else cfg.enabled_platforms()
    )

    failures = 0
    for name in platforms:
        if post.status.get(name) == "uploaded":
            print(f"- {name}: 이미 업로드됨, 건너뜀")
            continue
        print(f"- {name}: 업로드 중...")
        try:
            result = get_uploader(name).upload(cfg, post, draft_dir, dry_run=args.dry_run)
            print(f"  {result}")
            if not args.dry_run:
                post.status[name] = "uploaded"
        except UploadError as e:
            failures += 1
            print(f"  실패: {e}", file=sys.stderr)
            post.status[name] = f"failed:{e}"
    if not args.dry_run:
        post.save(draft_dir)
    return failures


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    slug = cmd_generate(cfg, args)
    return cmd_upload(cfg, args, slug=slug)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sns", description="SNS 자동화 파이프라인")
    parser.add_argument("--config", help="config.yaml 경로(기본: social-automation/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="콘텐츠 초안 생성")
    p_gen.add_argument("--topic", help="주제 직접 지정")
    p_gen.add_argument("--auto", action="store_true", help="config topics 큐에서 자동 선택")

    sub.add_parser("list", help="초안 목록")

    p_pre = sub.add_parser("preview", help="초안 미리보기")
    p_pre.add_argument("slug")

    p_up = sub.add_parser("upload", help="초안 업로드")
    p_up.add_argument("slug")
    p_up.add_argument("--platforms", help="쉼표 구분(기본: 설정에서 enabled 인 전체)")
    p_up.add_argument("--dry-run", action="store_true")

    p_run = sub.add_parser("run", help="생성+업로드 한 번에(cron/CI용)")
    p_run.add_argument("--topic")
    p_run.add_argument("--auto", action="store_true")
    p_run.add_argument("--platforms")
    p_run.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "generate":
        if not args.topic and not args.auto:
            parser.error("--topic 또는 --auto 중 하나를 지정하세요")
        cmd_generate(cfg, args)
    elif args.command == "list":
        cmd_list(cfg, args)
    elif args.command == "preview":
        cmd_preview(cfg, args)
    elif args.command == "upload":
        sys.exit(1 if cmd_upload(cfg, args) else 0)
    elif args.command == "run":
        if not args.topic and not args.auto:
            parser.error("--topic 또는 --auto 중 하나를 지정하세요")
        sys.exit(1 if cmd_run(cfg, args) else 0)


if __name__ == "__main__":
    main()
