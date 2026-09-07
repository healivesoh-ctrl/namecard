"""템플릿 변수 치환과 폼 항목 찾기."""

from __future__ import annotations

import re
from datetime import datetime

_VAR = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def find_field(answers: dict[str, str], keywords: list[str]) -> str | None:
    """항목 이름에 keywords 중 하나가 포함된 첫 항목의 키를 돌려준다 (대소문자 무시).

    정확히 일치하는 이름을 우선하고, 없으면 부분 일치.
    """
    if not answers or not keywords:
        return None
    lower = {k: k.lower() for k in answers}
    kws = [k.lower() for k in keywords if k]
    for k, lk in lower.items():
        if lk in kws:
            return k
    for kw in kws:
        for k, lk in lower.items():
            if kw in lk:
                return k
    return None


def lookup(answers: dict[str, str], key: str, builtins: dict[str, str] | None = None) -> str:
    """변수 이름으로 값 찾기: 내장 변수 → 정확 일치 → 부분 일치 순."""
    if builtins and key in builtins:
        return builtins[key]
    if key in answers:
        return str(answers[key])
    found = find_field(answers, [key])
    if found is not None:
        return str(answers[found])
    return ""


def render(template: str | None, answers: dict[str, str],
           builtins: dict[str, str] | None = None) -> str | None:
    """'{{이름}}님, {{과정명}} 접수' 형태의 문자열에 폼 응답을 채운다."""
    if template is None:
        return None

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        default = ""
        if "|" in key:  # {{항목|기본값}}
            key, default = [p.strip() for p in key.split("|", 1)]
        val = lookup(answers, key, builtins)
        return val if val else default

    return _VAR.sub(_sub, template)


def render_variables(spec: dict[str, str], answers: dict[str, str],
                     builtins: dict[str, str] | None = None) -> dict[str, str]:
    """템플릿 변수 정의({이름: "{{name}}"})를 실제 값으로 채운다."""
    return {k: (render(v, answers, builtins) or "") for k, v in spec.items()}


def builtin_vars(name: str, phone: str, now: datetime) -> dict[str, str]:
    from .phone import format_phone

    return {
        "name": name,
        "이름": name,
        "phone": format_phone(phone),
        "전화번호": format_phone(phone),
        "date": now.strftime("%Y-%m-%d"),
        "오늘": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
    }
