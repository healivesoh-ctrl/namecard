"""rules.yaml 로딩.

설정 파일 구조는 rules.example.yaml 참고. 환경변수(.env)는 발송사 인증정보 등
비밀값만 담고, 어떤 폼에 어떤 메시지를 언제 보낼지는 rules.yaml 에 적는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_FIELDS: dict[str, list[str]] = {
    "phone": ["전화번호", "휴대폰", "휴대전화", "연락처", "핸드폰", "전화", "phone", "mobile", "tel"],
    "name": ["이름", "성함", "성명", "name"],
    "consent": ["수신동의", "수신 동의", "마케팅", "알림톡 동의", "메시지 수신", "consent"],
}

AFFIRMATIVE = ("동의", "예", "네", "yes", "y", "true", "on", "1", "수신", "받겠", "checked", "ok")


@dataclass
class MessageRule:
    name: str
    template: str
    when: Any = "immediate"              # immediate | {delay: "1d"} | {at: "2026-09-20 09:00"} | {at_time: "09:00"} | {field_at: {...}}
    variables: dict[str, str] = field(default_factory=dict)
    text: str | None = None              # 완성 본문 (알리고 필수, 솔라피는 대체문자 본문)
    subject: str | None = None
    fallback: bool = True
    match: dict[str, Any] = field(default_factory=dict)
    buttons: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FormRule:
    id: str
    messages: list[MessageRule]
    match: dict[str, Any] = field(default_factory=dict)
    require_consent: bool = False
    dedupe_hours: float = 0             # 같은 번호가 N시간 내 재응답하면 발송 생략 (0=생략 안 함)
    fields: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Config:
    provider: str = "console"
    sender: str = ""
    kakao_channel: str = ""             # 솔라피 pfId / 알리고 senderkey
    timezone: str = "Asia/Seoul"
    quiet_hours: tuple[str, str] | None = None   # ("21:00", "08:00")
    fields: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_FIELDS))
    forms: list[FormRule] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def form(self, form_id: str | None) -> FormRule | None:
        """폼 id 로 규칙 찾기. 없으면 'default' 규칙, 그것도 없으면 첫 번째."""
        if not self.forms:
            return None
        for f in self.forms:
            if f.id == (form_id or "default"):
                return f
        for f in self.forms:
            if f.id == "default":
                return f
        return self.forms[0] if form_id is None else None

    def field_keywords(self, form: FormRule | None, kind: str) -> list[str]:
        merged: list[str] = []
        if form and form.fields.get(kind):
            merged += form.fields[kind]
        merged += self.fields.get(kind, [])
        return merged


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def parse_config(data: dict[str, Any]) -> Config:
    data = data or {}
    fields = dict(DEFAULT_FIELDS)
    for k, v in (data.get("fields") or {}).items():
        fields[k] = _as_list(v)
    qh = data.get("quiet_hours")
    quiet = None
    if isinstance(qh, dict) and qh.get("start") and qh.get("end"):
        quiet = (str(qh["start"]), str(qh["end"]))
    forms: list[FormRule] = []
    for f in data.get("forms") or []:
        msgs = []
        for m in f.get("messages") or []:
            msgs.append(MessageRule(
                name=str(m.get("name") or m.get("template")),
                template=str(m.get("template", "")),
                when=m.get("when", "immediate"),
                variables={str(k): str(v) for k, v in (m.get("variables") or {}).items()},
                text=m.get("text"),
                subject=m.get("subject"),
                fallback=bool(m.get("fallback", True)),
                match=m.get("match") or {},
                buttons=m.get("buttons") or [],
            ))
        forms.append(FormRule(
            id=str(f.get("id", "default")),
            messages=msgs,
            match=f.get("match") or {},
            require_consent=bool(f.get("require_consent", False)),
            dedupe_hours=float(f.get("dedupe_hours", 0) or 0),
            fields={k: _as_list(v) for k, v in (f.get("fields") or {}).items()},
        ))
    kakao = data.get("kakao") or {}
    return Config(
        provider=str(os.environ.get("ALIMTALK_PROVIDER") or data.get("provider") or "console"),
        sender=str(os.environ.get("ALIMTALK_SENDER") or data.get("sender") or ""),
        kakao_channel=str(os.environ.get("KAKAO_CHANNEL_ID") or kakao.get("channel_id")
                          or kakao.get("pf_id") or kakao.get("senderkey") or ""),
        timezone=str(data.get("timezone") or "Asia/Seoul"),
        quiet_hours=quiet,
        fields=fields,
        forms=forms,
        raw=data,
    )


def load_config(path: str | Path | None = None) -> Config:
    """rules.yaml 로드. 경로 우선순위: 인자 → RULES_PATH 환경변수 → rules.yaml → rules.example.yaml"""
    candidates = [path, os.environ.get("RULES_PATH"), BASE_DIR / "rules.yaml", BASE_DIR / "rules.example.yaml"]
    for c in candidates:
        if c and Path(c).exists():
            with open(c, encoding="utf-8") as fh:
                return parse_config(yaml.safe_load(fh) or {})
    return parse_config({})
