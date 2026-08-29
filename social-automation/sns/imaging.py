"""Pillow로 1080x1080 카드 이미지(그라데이션 배경 + 헤드라인)를 생성한다."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Config

SIZE = 1080

# 한글/중문을 지원하는 시스템 폰트 후보 (위에서부터 탐색)
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _find_font(cfg: Config, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    configured = cfg.image.get("font_path", "")
    candidates = ([configured] if configured else []) + FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # 마지막 수단: 시스템 폰트 디렉터리에서 CJK 폰트를 검색
    fonts_root = Path("/usr/share/fonts")
    if fonts_root.exists():
        for pattern in ("**/*CJK*", "**/Nanum*", "**/*Gothic*"):
            for found in sorted(fonts_root.glob(pattern)):
                if found.suffix.lower() in (".ttf", ".ttc", ".otf"):
                    try:
                        return ImageFont.truetype(str(found), size)
                    except OSError:
                        continue
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """픽셀 폭 기준 줄바꿈(한글·중문은 공백이 없을 수 있어 글자 단위 폴백)."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        current = ""
        for ch in para:
            trial = current + ch
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return [ln for ln in lines if ln != ""] or [""]


def make_card(
    cfg: Config,
    headline: str,
    subline: str,
    out_path: Path,
    color_top: str | None = None,
    color_bottom: str | None = None,
) -> Path:
    top = _hex_to_rgb(color_top or cfg.image.get("color_top", "#4F46E5"))
    bottom = _hex_to_rgb(color_bottom or cfg.image.get("color_bottom", "#0EA5E9"))

    img = Image.new("RGB", (SIZE, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        img.paste(Image.new("RGB", (SIZE, 1), row), (0, y))

    draw = ImageDraw.Draw(img)
    head_font = _find_font(cfg, 88)
    sub_font = _find_font(cfg, 44)
    brand_font = _find_font(cfg, 36)

    margin = 90
    max_width = SIZE - margin * 2

    head_lines = _wrap(draw, headline, head_font, max_width)
    sub_lines = _wrap(draw, subline, sub_font, max_width) if subline else []

    head_h = len(head_lines) * 108
    sub_h = len(sub_lines) * 62
    total_h = head_h + (30 + sub_h if sub_lines else 0)
    y = (SIZE - total_h) // 2

    for line in head_lines:
        draw.text((margin, y), line, font=head_font, fill="#FFFFFF")
        y += 108
    if sub_lines:
        y += 30
        for line in sub_lines:
            draw.text((margin, y), line, font=sub_font, fill="#E0F2FE")
            y += 62

    brand = cfg.brand.get("name", "")
    if brand:
        draw.text((margin, SIZE - margin - 40), brand, font=brand_font, fill="#FFFFFF")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92)
    return out_path
