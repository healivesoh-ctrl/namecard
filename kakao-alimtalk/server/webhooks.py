"""각 설문 서비스의 웹훅 페이로드를 {항목명: 답변} 형태로 정규화."""

from __future__ import annotations

from typing import Any


def _join(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(_join(x) for x in v if x is not None)
    if isinstance(v, dict):
        # Tally 의 옵션 객체 {"id":..,"text":..} / Typeform choice {"label":..}
        for k in ("text", "label", "value", "name"):
            if k in v:
                return str(v[k])
        return ", ".join(f"{k}: {x}" for k, x in v.items())
    return str(v)


def normalize_payload(body: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """반환: (answers, meta). meta 에는 phone/name 힌트와 폼 식별자가 들어갈 수 있다."""
    if not isinstance(body, dict):
        raise ValueError("JSON 객체가 아닙니다")
    meta: dict[str, Any] = {}

    # 1) 우리 Apps Script / 범용 형식: {"form": "...", "answers": {...}, "phone"?: "...", "name"?: "..."}
    if isinstance(body.get("answers"), dict):
        meta["form"] = body.get("form")
        meta["phone"] = body.get("phone")
        meta["name"] = body.get("name")
        return {str(k): _join(v) for k, v in body["answers"].items()}, meta

    # 2) Tally: {"eventType":"FORM_RESPONSE","data":{"formId":..,"formName":..,"fields":[{"label","value","type",...}]}}
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("fields"), list):
        answers: dict[str, str] = {}
        for f in data["fields"]:
            label = str(f.get("label") or f.get("key") or "")
            val = f.get("value")
            opts = f.get("options")
            if isinstance(val, list) and isinstance(opts, list):  # 선택형: id 목록 → 텍스트
                names = {o.get("id"): o.get("text") for o in opts if isinstance(o, dict)}
                val = [names.get(x, x) for x in val]
            if label:
                answers[label] = _join(val)
        meta["form"] = data.get("formId") or data.get("formName")
        return answers, meta

    # 3) Typeform: {"form_response":{"form_id","definition":{"fields":[{"id","title"}]},"answers":[{"field":{"id"},"type",...}]}}
    fr = body.get("form_response")
    if isinstance(fr, dict):
        titles = {f.get("id"): f.get("title") for f in (fr.get("definition") or {}).get("fields", [])}
        answers = {}
        for a in fr.get("answers") or []:
            fid = (a.get("field") or {}).get("id")
            title = str(titles.get(fid) or (a.get("field") or {}).get("ref") or fid or "")
            t = a.get("type")
            val = a.get(t) if t else None
            if t == "choice":
                val = (a.get("choice") or {}).get("label")
            elif t == "choices":
                val = (a.get("choices") or {}).get("labels")
            if t == "phone_number":
                meta["phone"] = val
            answers[title] = _join(val)
        meta["form"] = fr.get("form_id")
        return answers, meta

    # 4) 단순 평면 JSON {"이름": "...", "연락처": "..."} (Zapier/Make 등에서 직접 매핑)
    flat = {str(k): _join(v) for k, v in body.items() if not isinstance(v, (dict, list))}
    if flat:
        meta["form"] = body.get("form") or body.get("form_id")
        flat.pop("form", None); flat.pop("form_id", None); flat.pop("secret", None)
        return flat, meta
    raise ValueError("지원하지 않는 페이로드 형식입니다")
