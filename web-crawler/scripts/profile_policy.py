"""scripts/profile_policy.py — 프로필을 배포해도 되는가에 대한 단일 출처.

판정은 '무엇이 감지됐나(detection)' 가 아니라 '무엇으로 해결했나(response)' 를 본다.
antibot_type 은 신뢰할 수 없다 — 어떤 프로필은 그 값이 null 이면서 실제로는 6단 도구를
쓰고, 어떤 프로필은 WAF 가 감지됐다고 적혀 있으면서 실제로는 2단 API 호출로 끝냈다.

사다리:
    A(자동)  1 정적 HTML · 2 숨은 API · 3 JS 렌더링
    ─────── 이음매: 여기부터 상대가 나를 식별하고 거절한 상황이다 ───────
    B(통지)  4 지문 정렬 · 5 스텔스 브라우저 · 6 실제 크롬

사다리 B 도구로 해결한 프로필은 배포하지 않는다. 능력을 없애는 게 아니라 레시피를
배포에서 빼는 것이다.

도구에 관해 아는 것은 전부 `TOOLS` 한 표에 있고, 네 술어(`ladder_rung`,
`is_withheld_tool`, `is_unrecognized_tool`, `_has_non_string_field`)와 `infer_capability`
는 전부 거기서 파생된다. 예전에는 이름→칸 표와 이름→능력 표가 따로 있었고 둘이 어긋난
채로 한 번 배포됐다. 표가 하나면 어긋날 표가 없다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FINGERPRINTS = REPO_ROOT / "fingerprints"

# ── 도구 레지스트리 — 이름 하나당 행 하나 ────────────────────────────────────
#
# 키는 `_norm()` 을 거친 비교용 이름이다 (소문자, 영숫자 외 전부 제거).
# 값은 `(rung, capability, withheld)`:
#
#   rung        사다리 칸. 1~3 = 사다리 A(자동), 4~6 = 사다리 B(통지 후 진행).
#               0 = 이 값은 칸을 특정해주지 않는다 — 미상이 아니라 '칸 정보 없음' 이다.
#               `ladder_rung()` 이 max 를 취하므로 0 은 다른 필드의 칸을 가리지 않는다.
#   capability  능력 수준(CAPABILITIES 중 하나). None = 능력을 말해주지 않는 값.
#   withheld    우회는 아니지만 자동 배포 대상도 아닌 것. rung 0(모르겠다)과 다르다 —
#               이건 '모르는' 게 아니라 '알고서 뺀' 것이고, 따라서 `distribution: "public"`
#               선언으로도 풀리지 않는다.
#
# 여기 **없는** 문자열은 전부 미상이다 — 오타·신종·서술형뿐 아니라 `"-"`, `"N/A"`,
# `"???"` 같은 자리표시자도 포함한다. 자리표시자는 "대응이 필요 없었다" 는 정보가 아니라
# "적지 않았다" 는 공백이고, 공백을 정보로 읽는 것이 이 모듈이 막으려는 바로 그 fail-open
# 이다. 아래 중립값 두 줄(`none`/`null`)만이 "대응이 필요 없었다" 를 뜻한다.
TOOLS: dict[str, tuple[int, str | None, bool]] = {
    # ── 사다리 A 1단: 정적 HTML ──
    "fetcher":                (1, "static", False),
    "plainget":               (1, "static", False),
    "static":                 (1, "static", False),
    # Phase 0 공인 우회로 — 제공자가 명시적으로 여는 경로다. 사다리 A 의 가장 낮은 칸으로 둔다.
    "ytdlp":                  (1, "api", False),
    "rss":                    (1, "api", False),
    "oembed":                 (1, "api", False),
    # jina 는 r.jina.ai 에 평문 GET 한 번으로 렌더링된 본문을 그대로 받는다 — 우리 쪽에서
    # 보면 API 호출이 아니라 단일 정적 페이지 요청과 같은 모양이다.
    "jina":                   (1, "static", False),

    # ── 사다리 A 2단: 숨은 API ──
    "fetchersession":         (2, "api", False),
    "plainsession":           (2, "api", False),
    "apisession":             (2, "api", False),
    "api":                    (2, "api", False),

    # ── 사다리 A 3단: JS 렌더링 ──
    "dynamicfetcher":         (3, "js_render", False),
    "dynamicsession":         (3, "js_render", False),   # SKILL.md 무한 스크롤 절
    "playwrightfetcher":      (3, "js_render", False),   # DynamicFetcher 의 옛 이름 — 옛 프로필/문서가 아직 쓴다
    "playwright":             (3, "session", False),
    "playwrightspaintercept": (3, "session", False),
    "playwrightintercept":    (3, "session", False),
    "playwrightsyncapi":      (3, "session", False),     # 절대 규칙 1 예외 문구
    # Spider 는 등급을 올리는 도구가 아니라 동시성 래퍼다 — fetcher-patterns.md 예제가
    # AsyncFetcherSession 위에서 쓰는 것과 같이, 대량 API 호출을 감싸는 용도가 기본형이다.
    "spider":                 (3, "api", False),         # CLAUDE.md 500건+ 권고

    # ─────────────── 이음매: 아래부터는 상대가 나를 거절한 뒤의 대응이다 ───────────────

    # ── 사다리 B 4단: 지문 정렬 ──
    # curl_cffi 계열은 브라우저를 띄우지 않는다 — 지문을 맞춘 직접 엔드포인트 호출이므로
    # session/js_render 가 아니라 api 다.
    "curlcffi":               (4, "api", False),
    "curlcffigrid":           (4, "api", False),
    "grid":                   (4, "api", False),
    "impersonate":            (4, "api", False),         # 사이트가 평문 호출을 거절했을 때의 대응

    # ── 사다리 B 5단: 스텔스 브라우저 ──
    "stealthy":               (5, "js_render", False),
    "stealthyfetcher":        (5, "js_render", False),
    "stealthysession":        (5, "js_render", False),

    # ── 사다리 B 6단: 실제 크롬 ──
    "cdp":                    (6, "session", False),
    "chromecdp":              (6, "session", False),
    "naverantibot":           (6, "session", False),     # 네이버 계열 안티봇 — 실제 크롬 세션이 필요하다

    # ── 사다리 밖 ──
    # 자격증명 수집은 우회가 아니라서 칸이 없다(rung 0). 하지만 ToS 노출이 가장 큰 범주라
    # 배포에서는 명시적으로 뺀다 — 그래서 withheld 다.
    "authenticatedbrowser":   (0, "session", True),

    # 중립값 — "안티봇 대응이 필요 없었다" 는 **정보**다. 미상("모르겠다")과 취급이 다르다.
    # 실제 낱말만 여기 둔다. 자리표시자는 위 레지스트리 주석 참조 — 미상으로 간다.
    "none":                   (0, None, False),
    "null":                   (0, None, False),
}

# 능력 수준이 SSOT, 구체 fetcher 는 파생 —
# fetcher_type 에는 특정 라이브러리의 클래스 이름이 박혀 있고, 그 이름은 이미 한 번 바뀐
# 전력이 있다(PlayWrightFetcher -> DynamicFetcher). 엔진이 바뀌어도 프로필이 살아남게 한다.
CAPABILITIES = ("static", "js_render", "api", "session")

# 읽기 실패한 프로필에 넣는 표식 — TOOLS 에 없으므로 ladder_rung 이 0(미상)을 낸다
UNREADABLE = "__unreadable__"

# profile.get(field, _MISSING) 에서 '필드 자체가 없음' 과 '필드가 None' 을 구분하기 위한 표식.
_MISSING = object()

# 필드에 문자열이 아닌 값이 들어 있을 때의 정규화 결과 — '읽을 수 없음'.
# 미상(str 이지만 TOOLS 에 없음)과 구분한다: 미상은 선언으로 구제할 수 있지만 이건 못 한다.
_UNREADABLE_KEY = object()

_NORM_RE = re.compile(r"[^a-z0-9]")

# 판정에 쓰는 필드. antibot_type 은 의도적으로 뺐다 — 위 docstring 참조.
_FIELDS = ("fetcher_type", "antibot_strategy")


def _norm(value) -> str:
    """대소문자·공백·언더스코어·하이픈을 지운 비교용 키."""
    if not isinstance(value, str):
        return ""
    return _NORM_RE.sub("", value.lower())


def _field_keys(profile: dict) -> list:
    """판정 필드들을 셋 중 하나로 정규화한다 — 모든 술어가 오직 이것만 본다.

    술어마다 따로 필드를 훑던 시절에는 비문자열의 뜻이 술어마다 달랐다 —
    `ladder_rung` 은 닫히고(0) `is_withheld_tool` 은 열려서(False), 불변식이 두 술어의
    **호출 순서**로만 유지됐다. 정규화를 한 곳에 모아 그 순서 의존을 없앤다.

    반환 원소는 셋 중 하나:
        None             필드가 없거나 null — 정보 없음. 어느 술어도 이걸로 조이지 않는다.
        _UNREADABLE_KEY  문자열이 아님 — 읽을 수 없음. 모든 술어가 default-deny 로 받는다.
        str              정규화된 비교 키. TOOLS 에 있으면 알려진 값, 없으면 미상.
    """
    keys = []
    for field in _FIELDS:
        raw = profile.get(field, _MISSING)
        if raw is _MISSING or raw is None:
            keys.append(None)
        elif not isinstance(raw, str):
            keys.append(_UNREADABLE_KEY)
        else:
            keys.append(_norm(raw))
    return keys


def ladder_rung(profile: dict) -> int:
    """프로필이 기록한 해법이 앉은 사다리 칸. 판별 불가면 0.

    0 은 '안전하다' 가 아니라 '모르겠다' 다 — distribution() 이 default-deny 로 받는다.
    아무 필드도 칸을 알려주지 않으면(빈 프로필·중립값뿐·읽기 실패) 1 이 아니라 0 이다.
    """
    rungs = []
    for key in _field_keys(profile):
        if key is None:
            continue                     # 필드 없음 → 이 필드는 정보를 주지 않는다
        if key is _UNREADABLE_KEY:
            return 0                     # 문자열이 아니면 판별 불가 → default-deny
        entry = TOOLS.get(key)
        if entry is None:
            return 0                     # 모르는 값이 하나라도 있으면 판별 불가
        rungs.append(entry[0])            # 중립값·withheld 는 0 이라 다른 필드의 칸을 가리지 않는다
    return max(rungs) if rungs else 0


def is_withheld_tool(profile: dict) -> bool:
    """선언으로도 풀 수 없는 사유로 자동 배포에서 빼야 하는가.

    두 가지가 여기 걸린다:
    - withheld 로 표시된 도구 (예: authenticated_browser — 자격증명 수집은 ToS 노출이 가장
      큰 범주다). '모르는' 게 아니라 '알고서 뺀' 것이라 rung 0 과 구분된다.
    - 읽을 수 없는 값(문자열 아님). 예전에는 여기서 False 를 돌려주고(fail-open)
      distribution() 이 `_has_non_string_field` 를 **먼저** 호출해준 덕에만 안전했다.
      지금은 술어 자체가 닫힌다 — 호출 순서가 바뀌어도 불변식이 유지된다.
    """
    for key in _field_keys(profile):
        if key is None:
            continue
        if key is _UNREADABLE_KEY:
            return True
        entry = TOOLS.get(key)
        if entry is not None and entry[2]:
            return True
    return False


def is_unrecognized_tool(profile: dict) -> bool:
    """존재하지만 레지스트리에 없는 값을 쓰고 있는가.

    '없는 필드'(정보 없음)와 '있는데 모르는 값'(오타·신종·서술형·자리표시자)은 다르다.
    후자는 이음매를 건넜는지 판단할 수 없다는 뜻이므로, 게이트는 걸어야 한다 —
    ladder_rung 은 둘 다 0 으로 돌려주기 때문에 그것만으로는 구분되지 않는다.
    """
    for key in _field_keys(profile):
        if key is None:
            continue
        if key is _UNREADABLE_KEY or key not in TOOLS:
            return True
    return False


def _has_non_string_field(profile: dict) -> bool:
    """판별 대상 필드에 문자열이 아닌 값이 들어 있는가.

    있으면 그 프로필은 우리가 읽을 수 있는 형태가 아니다 — 선언으로도 풀지 않는다.
    `is_withheld_tool` 도 같은 답을 내므로 distribution() 안에서는 중복이지만, 이 조건은
    남겨 둔다 — '읽을 수 없음' 은 '빼기로 한 도구' 와 다른 사유이고, 그 둘을 한 술어로
    합쳐버리면 왜 local 인지가 호출부에서 안 보인다.
    """
    return any(key is _UNREADABLE_KEY for key in _field_keys(profile))


def distribution(profile: dict) -> str:
    """"public" | "local".

    선언은 **조이는 방향으로만 무조건 인정**한다. 푸는 방향은 사다리 A 또는 판별 불가
    (rung <= 3)에 대해서만 허용한다 — 사다리 B 라고 '판정된' 프로필을 한 단어로 뒤집을 수
    있으면, default-deny 정책 전체가 그 단어만큼만 강해진다.

    아래 세 검사는 순서에 기대지 않는다 — 각 술어가 스스로 default-deny 로 닫힌다.
    """
    declared = profile.get("distribution")
    if declared == "local":
        return "local"

    if _has_non_string_field(profile):
        return "local"          # 판별 불가 입력 — 선언으로 풀 수 없다

    if is_withheld_tool(profile):
        return "local"          # 선언으로 풀 수 없다

    rung = ladder_rung(profile)
    if declared == "public" and rung <= 3:
        return "public"
    return "local" if (rung == 0 or rung >= 4) else "public"


def is_distributable(profile: dict) -> bool:
    return distribution(profile) == "public"


def infer_capability(profile: dict) -> str | None:
    """capability 필드 우선, 없으면 fetcher_type 에서 역추론. 모르면 None."""
    declared = profile.get("capability")
    if declared in CAPABILITIES:
        return declared
    key = _field_keys(profile)[_FIELDS.index("fetcher_type")]
    if not isinstance(key, str):
        return None
    entry = TOOLS.get(key)
    return entry[1] if entry else None


def load_all() -> dict[str, dict]:
    """fingerprints/*/profile.json 전체를 {디렉터리명: dict} 로.

    읽지 못한 프로필은 '내용이 없는 프로필' 이 아니라 '미상' 이다 — 빈 dict 를 넣으면
    ladder_rung 이 1(=정적 HTML)로 읽어 배포 대상이 되어버린다. 명시적으로 미상 표식을 넣는다.
    """
    out = {}
    if not FINGERPRINTS.is_dir():
        return out
    for path in sorted(FINGERPRINTS.glob("*/profile.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            out[path.parent.name] = data if isinstance(data, dict) else {"fetcher_type": UNREADABLE}
        except (ValueError, OSError):
            out[path.parent.name] = {"fetcher_type": UNREADABLE}   # 미상 → default-deny
    return out


def public_dirs() -> list[str]:
    """배포 대상 디렉터리명 정렬 목록."""
    return sorted(name for name, profile in load_all().items() if is_distributable(profile))
