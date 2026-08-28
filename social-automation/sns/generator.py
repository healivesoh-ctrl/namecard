"""Claude API로 플랫폼별 콘텐츠를 한 번에 생성한다.

한 주제(topic)를 입력하면 아래 구조의 JSON을 받아 Post 로 변환한다.
  - instagram:   한국어 캡션 + 해시태그
  - xiaohongshu: 중국어 제목/본문 + 태그 (샤오홍슈 특유의 이모지·구어체 스타일)
  - naver_blog:  한국어 장문 SEO 포스트(제목/본문/태그)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

import anthropic

from .config import Config
from .models import PlatformContent, Post

SYSTEM_PROMPT = """\
당신은 멀티플랫폼 SNS 콘텐츠 전문가입니다. 하나의 주제를 받아
인스타그램·샤오홍슈(小红书)·네이버블로그 각각의 문법에 맞는 콘텐츠를 만듭니다.

플랫폼별 스타일 규칙:
- instagram: 첫 줄 훅(hook)으로 시작하는 한국어 캡션. 짧은 문단, 줄바꿈 활용,
  이모지 적당히. 캡션 본문에는 해시태그를 넣지 말 것(별도 배열로 반환).
- xiaohongshu: 중국어(简体). 제목은 20자 이내, 이모지 1~2개 포함.
  본문은 소홍서 특유의 친근한 구어체, 줄마다 짧게, 이모지·기호(✅ ❗ 💡) 활용.
- naver_blog: 한국어 장문 정보성 포스트. 검색 노출(SEO)을 고려해 제목에 핵심
  키워드 포함. 본문은 소제목(## 마크다운)으로 구획을 나누고, 도입-본문-마무리
  구조. 지정된 최소 글자 수 이상.

반드시 아래 JSON 스키마로만 응답하세요. JSON 외의 설명 텍스트를 붙이지 마세요.
{
  "instagram":   {"caption": "...", "hashtags": ["태그1", ...]},
  "xiaohongshu": {"title": "...", "body": "...", "tags": ["标签1", ...]},
  "naver_blog":  {"title": "...", "body": "...", "tags": ["태그1", ...]},
  "image_text":  {"headline": "카드 이미지에 크게 넣을 한 줄(15자 이내)",
                  "subline": "보조 문구 한 줄(25자 이내)"}
}
"""


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w가-힣一-鿿-]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "post"


def _extract_json(text: str) -> dict:
    """응답에서 JSON 오브젝트만 추출한다(코드펜스·설명이 섞여도 복구)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"응답에서 JSON을 찾지 못했습니다:\n{text[:500]}")
    return json.loads(text[start : end + 1])


def build_user_prompt(cfg: Config, topic: str) -> str:
    brand = cfg.brand
    ig = cfg.platform("instagram")
    xhs = cfg.platform("xiaohongshu")
    nb = cfg.platform("naver_blog")
    return f"""\
브랜드 정보:
- 이름: {brand.get('name', '')}
- 소개: {brand.get('description', '')}
- 톤앤매너: {brand.get('tone', '')}
- 타깃 독자: {brand.get('audience', '')}
- 웹사이트: {brand.get('website', '')}

오늘의 주제: {topic}

세부 요구사항:
- instagram 해시태그 {ig.get('hashtag_count', 15)}개 (한국어+영어 혼합, '#' 제외한 단어만)
- xiaohongshu 태그 {xhs.get('tag_count', 8)}개 (중국어, '#' 제외)
- naver_blog 본문 {nb.get('min_chars', 1500)}자 이상, 태그 {nb.get('tag_count', 10)}개
- 과장 광고 문구(최고, 1위 등 근거 없는 주장) 금지
"""


def generate_post(cfg: Config, topic: str) -> tuple[Post, dict]:
    """콘텐츠를 생성해 (Post, 카드 이미지 문구 dict)를 반환한다."""
    client = anthropic.Anthropic()

    # 서버사이드 refusal fallback을 기본 활성화(정책상 거절 시 대체 모델이 이어서 처리)
    with client.beta.messages.stream(
        model=cfg.model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(cfg, topic)}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise RuntimeError(f"모델이 요청을 거절했습니다: {detail}")

    text = "".join(b.text for b in response.content if b.type == "text")
    data = _extract_json(text)

    today = dt.date.today().isoformat()
    slug = f"{today}-{slugify(topic)}"
    post = Post(topic=topic, slug=slug, created_at=dt.datetime.now().isoformat())

    ig_data = data.get("instagram", {})
    post.contents["instagram"] = PlatformContent(
        platform="instagram",
        body=ig_data.get("caption", ""),
        hashtags=list(ig_data.get("hashtags", [])),
    )
    xhs_data = data.get("xiaohongshu", {})
    post.contents["xiaohongshu"] = PlatformContent(
        platform="xiaohongshu",
        title=xhs_data.get("title", ""),
        body=xhs_data.get("body", ""),
        hashtags=list(xhs_data.get("tags", [])),
    )
    nb_data = data.get("naver_blog", {})
    post.contents["naver_blog"] = PlatformContent(
        platform="naver_blog",
        title=nb_data.get("title", ""),
        body=nb_data.get("body", ""),
        hashtags=list(nb_data.get("tags", [])),
    )
    for name in cfg.enabled_platforms():
        post.status[name] = "draft"

    return post, data.get("image_text", {})
