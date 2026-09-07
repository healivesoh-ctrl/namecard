"""scripts/test_domain_profile.py — DomainProfile 단위 테스트"""
import json
import os
import time
from datetime import datetime, timezone
from domain_profile import DomainProfile


def test_save_and_load_profile(tmp_path):
    profile = DomainProfile(str(tmp_path))
    data = {
        "domain": "example.com",
        "fetcher_type": "Fetcher",
        "selectors": {"title": "h2::text", "price": ".price::text"},
        "pagination": {"type": "url_param", "param": "page"},
    }
    profile.save("example.com", data)
    loaded = profile.load("example.com")
    assert loaded["fetcher_type"] == "Fetcher"
    assert loaded["selectors"]["title"] == "h2::text"


def test_load_nonexistent(tmp_path):
    profile = DomainProfile(str(tmp_path))
    assert profile.load("nonexistent.com") is None


def test_profile_exists(tmp_path):
    profile = DomainProfile(str(tmp_path))
    profile.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    assert profile.exists("example.com") is True
    assert profile.exists("other.com") is False


def test_distribution_declaration_survives_resave(tmp_path):
    """Step 5-A 는 매번 새 dict 를 넘긴다 — 선언이 거기서 지워지면 안 된다."""
    from domain_profile import DomainProfile
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Playwright",
                             "distribution": "local", "distribution_reason": "robots",
                             "consent": {"notified_at": "2026-08-20T00:00:00+09:00",
                                         "choice": "proceed"}})
    # consent 도 sticky 다 — 두 번째 저장은 다시 넘기지 않아도 앞선 결정을 물려받는다.
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Playwright"})
    reloaded = mgr.load("example.com")
    assert reloaded["distribution"] == "local"
    assert reloaded["distribution_reason"] == "robots"


def test_caller_can_still_change_distribution(tmp_path):
    """보존이지 고정이 아니다 — 명시적으로 넘기면 그 값이 이긴다."""
    from domain_profile import DomainProfile
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher",
                             "distribution": "local", "distribution_reason": "x",
                             "consent": {"notified_at": "2026-08-20T00:00:00+09:00",
                                         "choice": "proceed"}})
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher",
                             "distribution": "public", "distribution_reason": "y"})
    assert mgr.load("example.com")["distribution"] == "public"


def test_save_survives_corrupt_existing_profile(tmp_path):
    """Step 5-A 는 수집 성공 직후다 — 기존 파일이 깨졌다고 여기서 죽으면 안 된다."""
    from domain_profile import DomainProfile
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    (target / "profile.json").write_text("{not json", encoding="utf-8")
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    assert mgr.load("example.com")["fetcher_type"] == "Fetcher"


def test_save_and_load_handle_bom(tmp_path):
    """PowerShell 이 기본으로 BOM 을 붙인다 — 손으로 고친 프로필이 읽혀야 한다."""
    from domain_profile import DomainProfile
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    (target / "profile.json").write_text(
        '{"domain": "example.com", "distribution": "local"}', encoding="utf-8-sig")
    assert mgr.load("example.com")["distribution"] == "local"
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher",
                             "consent": {"notified_at": "2026-08-20T00:00:00+09:00",
                                         "choice": "proceed"}})
    assert mgr.load("example.com")["distribution"] == "local"   # sticky 가 BOM 파일에서도 보존


def test_save_survives_ansi_encoded_existing_profile(tmp_path):
    """CLAUDE.md 가 경고하는 대로 Set-Content 는 ANSI 로 쓴다 — 한글 notes 가 그 지뢰다."""
    from domain_profile import DomainProfile
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    (target / "profile.json").write_bytes(
        '{"domain": "example.com", "notes": "한글 메모"}'.encode("cp949"))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    assert mgr.load("example.com")["fetcher_type"] == "Fetcher"


# ── S3: capability SSOT ──
from profile_policy import infer_capability


def test_capability_field_wins():
    assert infer_capability({"capability": "api", "fetcher_type": "Fetcher"}) == "api"


def test_capability_inferred_from_fetcher_type():
    """마이그레이션 호환 — capability 가 없어도 기존 프로필이 그대로 산다."""
    assert infer_capability({"fetcher_type": "Fetcher"}) == "static"
    assert infer_capability({"fetcher_type": "FetcherSession"}) == "api"
    assert infer_capability({"fetcher_type": "DynamicFetcher"}) == "js_render"
    assert infer_capability({"fetcher_type": "playwright_spa_intercept"}) == "session"
    assert infer_capability({"fetcher_type": "chrome_cdp"}) == "session"


def test_capability_unknown_returns_none():
    assert infer_capability({"fetcher_type": "SomeNewThing"}) is None


def test_all_tracked_profiles_declare_capability():
    """마이그레이션이 끝났는지 — 배포되는 프로필은 전부 capability 를 갖는다."""
    from profile_policy import is_distributable, load_all
    missing = [name for name, p in load_all().items()
               if is_distributable(p) and "capability" not in p]
    assert missing == [], f"capability 필드가 없는 배포 프로필: {missing}"


def test_capability_survives_resave(tmp_path):
    """capability 도 sticky 다 — Step 5-A 가 새 dict 를 넘겨도 지워지면 안 된다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Playwright",
                             "capability": "session"})
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Playwright"})
    assert mgr.load("example.com")["capability"] == "session"


def test_antibot_strategy_survives_resave(tmp_path):
    """분류를 결정하는 필드가 생략만으로 바뀌면, 미배포 결정이 조용히 뒤집힌다."""
    from profile_policy import distribution
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "FetcherSession",
        "antibot_strategy": "impersonate",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    })
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "FetcherSession"})
    reloaded = mgr.load("example.com")
    assert reloaded["antibot_strategy"] == "impersonate"
    assert distribution(reloaded) == "local"


def test_caller_can_still_clear_antibot_strategy(tmp_path):
    """보존이지 고정이 아니다 — 명시적으로 넘기면 그 값이 이긴다."""
    from profile_policy import distribution
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "FetcherSession",
        "antibot_strategy": "impersonate",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    })
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "FetcherSession",
                             "antibot_strategy": "none"})
    assert distribution(mgr.load("example.com")) == "public"


def test_save_does_not_mutate_callers_dict(tmp_path):
    """save() 는 호출자의 dict 를 건드리지 않는다 — ITEM 5 의 pop 이 이걸 load-bearing 으로 만들었다."""
    import copy
    mgr = DomainProfile(base_dir=str(tmp_path))
    caller = {
        "domain": "example.com", "fetcher_type": "FetcherSession",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    }
    snapshot = copy.deepcopy(caller)
    mgr.save("example.com", caller)
    assert caller == snapshot


# ── P2-4: consent 게이트 ──
import pytest

from domain_profile import ConsentRequired, DomainProfile


def test_ladder_a_profile_saves_without_consent(tmp_path):
    """탐색 단계 프로필은 통지 대상이 아니다 — 아무것도 요구하지 않는다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    assert mgr.load("example.com")["fetcher_type"] == "Fetcher"


def test_ladder_b_profile_requires_consent(tmp_path):
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired) as exc:
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp"})
    assert "consent" in str(exc.value)


def test_ladder_b_profile_saves_with_consent(tmp_path):
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com",
        "fetcher_type": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T14:30:00+09:00", "choice": "proceed"},
    })
    assert mgr.load("example.com")["consent"]["choice"] == "proceed"


def test_consent_must_record_a_choice(tmp_path):
    """빈 블록으로 게이트를 통과할 수 없다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp",
                                 "consent": {}})


def test_consent_does_not_require_a_justification(tmp_path):
    """원칙 ④ — 근거를 제출받지 않는다. 선택과 시각만 있으면 충분하다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com",
        "fetcher_type": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T14:30:00+09:00", "choice": "proceed"},
    })   # authorization_basis 없이도 저장된다


def test_consent_survives_resave(tmp_path):
    """이미 내린 결정을 매 수집마다 다시 묻지 않는다 — 프로필이 그 기록을 지닌다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    })
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp"})
    assert mgr.load("example.com")["consent"]["choice"] == "proceed"


def test_first_escalation_still_requires_consent(tmp_path):
    """게이트는 여전히 발화한다 — 프로필이 없는 도메인에는 상속할 기록이 없다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("brand-new.example", {"domain": "brand-new.example",
                                       "fetcher_type": "chrome_cdp"})


def test_local_declaration_via_sticky_merge_saves_without_consent(tmp_path):
    """robots/ToS 사유의 `distribution: local` 선언은 이음매를 넘은 기록이 아니다 —
    rung 1(탐색)에 머무르는 한 sticky 병합을 통해 넘어와도 consent 를 요구하지 않는다.

    ITEM 6: 게이트 조건이 distribution(profile)=="local" 에서 ladder_rung(profile)>=4 로
    좁혀졌다. declare-local(robots 로 거른 것)과 이음매 통지 기록(WAF 를 실제로 돌파한 것)은
    서로 다른 결정이므로 더 이상 하나로 묶이지 않는다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    (target / "profile.json").write_text(
        '{"domain": "example.com", "distribution": "local", '
        '"distribution_reason": "robots", "fetcher_type": "Fetcher"}', encoding="utf-8")
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    reloaded = mgr.load("example.com")
    assert reloaded["distribution"] == "local"
    assert "consent" not in reloaded


def test_consent_required_when_sticky_antibot_strategy_has_no_consent_to_inherit(tmp_path):
    """rung 판정이 sticky 병합으로만 넘어와도 게이트는 걸린다 — 상속할 consent 가 없기 때문이다.

    이 테스트가 잡는 회귀: 게이트를 병합 전 caller dict 에 대해 계산하도록 바꾼 변형은
    이 케이스를 그냥 통과시킨다 — merge 전 profile 은 fetcher_type: "FetcherSession" 하나뿐이라
    rung 2(공개)로 보이기 때문이다. 실제로 저장될 profile 은 sticky 로 antibot_strategy:
    "impersonate"(rung 4)를 물려받는데도 게이트가 발화하지 않는다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    (target / "profile.json").write_text(
        '{"domain": "example.com", "antibot_strategy": "impersonate", '
        '"fetcher_type": "FetcherSession"}', encoding="utf-8")
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "FetcherSession"})


def test_omitted_fetcher_type_saves_without_consent_but_stays_local(tmp_path):
    """rung 미상(0)은 "이음매를 넘었다" 는 기록이 아니다 — consent 를 요구하지 않는다.

    배포 판정은 여전히 default-deny 라 local 로 남는다(profile_policy.distribution) — 게이트가
    느슨해진 것과 배포되지 않는 것은 별개다."""
    from profile_policy import distribution as _distribution
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com"})
    reloaded = mgr.load("example.com")
    assert "consent" not in reloaded
    assert _distribution(reloaded) == "local"


def test_authenticated_browser_saves_without_consent_but_stays_local(tmp_path):
    """withheld 도구(ladder_rung 미상)는 이음매를 넘은 게 아니다 — consent 를 요구하지 않으면서도
    local 로 남는다(자격증명 수집은 ToS 노출이 가장 큰 범주라 별도로 배포에서 제외된다)."""
    from profile_policy import distribution as _distribution
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "authenticated_browser"})
    reloaded = mgr.load("example.com")
    assert "consent" not in reloaded
    assert _distribution(reloaded) == "local"


def test_rung_four_profile_requires_consent(tmp_path):
    """4단(지문 정렬)도 6단과 마찬가지로 이음매를 넘은 것이다 — consent 없이는 거부된다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "FetcherSession",
                                 "antibot_strategy": "curl_cffi_grid"})


# ── ladder_rung 이 0 으로 뭉갠 "판별 불가"(정보 없음) / "모르는 값"(오타·신종·서술형) 을
# is_unrecognized_tool 로 구분한다. 이 구분이 없으면 ITEM 6 이 좁힌 게이트 조건
# (ladder_rung(profile) >= 4) 이 모르는 값을 쓰는 프로필을 전부 통과시킨다 — 그 프로필을
# 쓰는 사람이 바로 통지를 건너뛴 사람과 가장 많이 겹치는 인구다. ──

@pytest.mark.parametrize("profile_extra", [
    {"fetcher_type": "chrome_cdp", "antibot_strategy": "some_typo_value"},
    {"fetcher_type": "fetch_via_grid"},
    {"fetcher_type": "StealthyFetcher(solve_cloudflare=True)"},
    {"fetcher_type": "CDP (headed)"},
    {"fetcher_type": ["chrome_cdp"]},   # 비문자열도 판별 불가와 구분 없이 잡혀야 한다
])
def test_unrecognized_tool_still_requires_consent(tmp_path, profile_extra):
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", **profile_extra})


def test_unrecognized_tool_saves_once_consent_is_recorded(tmp_path):
    """모르는 값이라도 통지가 실제로 있었으면 저장은 진행된다 — 게이트는 심사가 아니라 기록."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "fetch_via_grid",
        "consent": {"notified_at": "2026-08-20T14:30:00+09:00", "choice": "proceed"},
    })
    assert mgr.load("example.com")["fetcher_type"] == "fetch_via_grid"


# ── ITEM 2 (fix round 3): 예외 메시지가 발화 원인에 맞는 처방을 준다 ──

def test_genuine_rung_four_message_tells_you_to_record_consent(tmp_path):
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired) as exc:
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp"})
    assert "consent" in str(exc.value)
    assert "고쳐라" not in str(exc.value)   # 진짜 4단 이상은 값을 고치라고 하면 안 된다


def test_unrecognized_value_message_tells_you_to_fix_the_value_not_consent(tmp_path):
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired) as exc:
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "fetch_via_grid"})
    message = str(exc.value)
    assert "고쳐라" in message
    # 진짜 4단 이상 메시지와 달리, 모르는 값 메시지는 "통지를 기록하라" 는 처방을 주지 않는다.
    assert "그 선택과 통지한 실제" not in message


def test_the_two_branches_produce_different_messages(tmp_path):
    """같은 예외 타입이라도 발화 원인이 다르면 안내가 달라야 한다 — 헷갈리면 안 된다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired) as exc_genuine:
        mgr.save("a.example", {"domain": "a.example", "fetcher_type": "chrome_cdp"})
    with pytest.raises(ConsentRequired) as exc_unrecognized:
        mgr.save("b.example", {"domain": "b.example", "fetcher_type": "fetch_via_grid"})
    assert str(exc_genuine.value) != str(exc_unrecognized.value)


def test_relaxations_still_hold_after_unrecognized_tool_check(tmp_path):
    """ITEM 1 백스톱을 추가해도 fix round 1 에서 열어둔 완화들은 그대로 유지된다."""
    from profile_policy import distribution as _distribution
    mgr = DomainProfile(base_dir=str(tmp_path))

    # authenticated_browser — withheld, 이음매를 넘은 게 아니다.
    mgr.save("auth.example", {"domain": "auth.example", "fetcher_type": "authenticated_browser"})
    assert "consent" not in mgr.load("auth.example")

    # rung 1 + robots 사유의 distribution: local — 이음매를 넘은 게 아니다.
    mgr.save("robots.example", {"domain": "robots.example", "fetcher_type": "Fetcher",
                                "distribution": "local", "distribution_reason": "robots"})
    reloaded = mgr.load("robots.example")
    assert reloaded["distribution"] == "local"
    assert "consent" not in reloaded

    # 필드 자체가 없는 최초 진입 — 정보가 없을 뿐 모르는 값을 쓴 게 아니다.
    mgr.save("bare.example", {"domain": "bare.example"})
    reloaded = mgr.load("bare.example")
    assert "consent" not in reloaded
    assert _distribution(reloaded) == "local"


@pytest.mark.parametrize("malformed_consent", ["proceed", ["proceed"], 123])
def test_malformed_consent_raises_consent_required_not_a_traceback(tmp_path, malformed_consent):
    """consent 가 잘못된 형태(문자열/리스트/숫자)여도 안내 메시지로 떨어진다 —
    AttributeError 트레이스백이 아니라."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp",
                                 "consent": malformed_consent})


@pytest.mark.parametrize("choice", ["decline", "maybe-later"])
def test_consent_choice_must_be_proceed(tmp_path, choice):
    """'진행을 골랐다' 는 사실만 인정한다 — 그 외 choice 값은 게이트를 통과시키지 않는다.

    {"choice": "decline"} 가 그냥 저장되면, 우회 레시피 옆에 '사용자가 거부했다' 는
    기록이 나란히 남는 자기모순적인 산출물이 된다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp",
                                 "consent": {"notified_at": "2026-08-20T00:00:00+09:00",
                                             "choice": choice}})


# ── ITEM 5(b): notified_at 자리표시자/빈 값은 통지의 증거가 아니다 ──

@pytest.mark.parametrize("bad_notified_at", ["", None, "<ISO8601>", "<통지한 실제 시각 ISO8601>",
                                             "통지한 실제 시각 ISO8601"])
def test_consent_rejects_placeholder_or_missing_notified_at(tmp_path, bad_notified_at):
    """Step 5-A 템플릿을 그대로 복사해 notified_at 을 자리표시자로 남기면 게이트를 통과할
    수 없다 — choice 만 "proceed" 여도, 실제로 통지했다는 증거가 되지 못한다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp",
                                 "consent": {"notified_at": bad_notified_at,
                                             "choice": "proceed"}})


def test_consent_accepts_real_timestamp(tmp_path):
    """자리표시자가 아닌 실제 시각 문자열은 정상적으로 통과한다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T14:30:00+09:00", "choice": "proceed"},
    })
    assert mgr.load("example.com")["consent"]["notified_at"] == "2026-08-20T14:30:00+09:00"


def test_consent_dropped_when_profile_downgrades_to_public(tmp_path):
    """consent 는 통지 이력이다 — public 으로 재분류된 프로필에 남아있으면 안 된다.

    fetcher_type 은 sticky 가 아니므로, 같은 도메인을 사다리 A 도구로 다시 수집하면
    분류가 public 으로 내려간다. 그때 이전 consent 가 남아 있으면, 배포 whitelist 에
    들어가는 프로필이 이 사용자의 통지 이력을 실어 나르게 된다."""
    from profile_policy import distribution as _distribution
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    })
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})
    reloaded = mgr.load("example.com")
    assert "consent" not in reloaded
    assert _distribution(reloaded) == "public"


def test_seam_recrossing_asks_again(tmp_path):
    """B → A → B 로 돌아온 도메인은 다시 통지를 요구한다 — 왕복 전체를 건다.

    위 테스트는 내려가는 쪽(consent 가 지워진다)만 본다. 문서가 말하는 규칙은 그 다음
    절반이다: **통지는 도메인당 1회가 아니라 이음매를 통과할 때마다 1회다.** 면제하는 것은
    도메인이 아니라 프로필이 *지금 들고 있는* consent 기록이고, 그 기록은 프로필이 배포
    대상이 되는 순간 지워지므로 — 사용자의 통지 이력을 배포되는 파일에 실어 보내지 않기
    위해 — 사이트가 나중에 새 보호를 걸면 들고 있는 기록이 없다.

    이 왕복이 테스트로 고정돼 있지 않으면 "sticky 니까 한 번이면 된다" 는 오독이 조용히
    돌아온다. 실제로 SKILL.md 는 이 규칙을 '도메인당 한 번' 이라고 잘못 적고 있었다.
    """
    mgr = DomainProfile(base_dir=str(tmp_path))

    # ① 최초 이음매 통과 — 통지하고 기록한다
    mgr.save("example.com", {
        "domain": "example.com", "fetcher_type": "chrome_cdp",
        "antibot_strategy": "chrome_cdp",
        "consent": {"notified_at": "2026-08-20T00:00:00+09:00", "choice": "proceed"},
    })
    assert mgr.load("example.com")["consent"]["choice"] == "proceed"

    # ② 사이트가 차단을 풀어 사다리 A 로 내려온다 — 배포 대상이 되며 기록이 지워진다.
    #    antibot_strategy 는 sticky 라 생략하면 상속되므로, 실제로 내려왔음을 명시한다.
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher",
                             "antibot_strategy": "none"})
    assert "consent" not in mgr.load("example.com")

    # ③ 사이트가 새 보호를 건다 — 들고 있는 기록이 없으므로 게이트가 다시 발화한다
    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp",
                                 "antibot_strategy": "chrome_cdp"})


# ── P2-4 백스톱 ②: 손상된 기존 프로필을 조용히 덮어쓰지 않는다 ──

def test_save_backs_up_corrupt_profile_before_overwrite(tmp_path):
    """깨진 파일을 조용히 덮어쓰면 그 안의 distribution 선언이 흔적 없이 사라진다.
    백업하고 경고한 뒤 진행해야 한다."""
    fixed_now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    mgr = DomainProfile(base_dir=str(tmp_path), clock=lambda: fixed_now)
    target = tmp_path / "example_com"
    target.mkdir()
    corrupt_bytes = b"{not json"
    (target / "profile.json").write_bytes(corrupt_bytes)

    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "Fetcher"})

    # (a) 저장은 여전히 성공한다
    assert mgr.load("example.com")["fetcher_type"] == "Fetcher"
    # (b) 원본이 타임스탬프가 박힌 백업 파일로 보존된다 (초 미만 정밀도 — 동일 초 충돌 방지)
    backup = target / "profile.json.corrupt-20260820T120000.000000Z"
    assert backup.exists()
    # (c) 시각은 주입한 clock 에서 나온다 — wall-clock 이 아니다
    assert backup.read_bytes() == corrupt_bytes


def test_refused_save_leaves_corrupt_existing_file_intact(tmp_path):
    """게이트가 거부하면 손상된 기존 파일에도 손대지 않는다 — 백업도, 삭제도 하지 않는다.

    거부된 저장이 실제로 파일을 건드리면, "저장 실패" 가 "프로필 소실" 로 바뀐다 —
    거부 직후 exists() 가 False 를 보고하게 되어버린다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    target = tmp_path / "example_com"
    target.mkdir()
    corrupt_bytes = b"{not json"
    (target / "profile.json").write_bytes(corrupt_bytes)

    with pytest.raises(ConsentRequired):
        mgr.save("example.com", {"domain": "example.com", "fetcher_type": "chrome_cdp"})

    assert (target / "profile.json").read_bytes() == corrupt_bytes
    assert list(target.glob("profile.json.corrupt-*")) == []


def test_save_fills_capability_when_absent(tmp_path):
    """capability 는 SSOT 인데 저장 템플릿을 따라도 비는 경우가 있었다 — save() 가 채운다.

    마이그레이션된 옛 프로필들만 이 필드를 갖고 새 도메인은 영영 못 갖는 상태였다. 그러면
    읽는 쪽이 fetcher_type 역추론 폴백에만 기대게 되는데, domain_profile.py 의 STICKY_FIELDS
    주석이 바로 그 폴백에 기대지 말라고 적어 둔 것이다.
    """
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com", "fetcher_type": "DynamicFetcher"})
    assert mgr.load("example.com")["capability"] == "js_render"


def test_save_does_not_overwrite_an_explicit_capability(tmp_path):
    """호출자가 적은 값이 이긴다 — capability 가 SSOT 이고 fetcher_type 이 파생이므로."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {
        "domain": "example.com",
        "capability": "api",
        "fetcher_type": "DynamicFetcher",   # 역추론이면 js_render 가 나온다
    })
    assert mgr.load("example.com")["capability"] == "api"


def test_save_leaves_capability_absent_when_inference_fails(tmp_path):
    """추론이 안 되면 지어내지 않는다 — 없는 것과 틀린 것 중에서는 없는 쪽이 낫다."""
    mgr = DomainProfile(base_dir=str(tmp_path))
    mgr.save("example.com", {"domain": "example.com"})
    assert "capability" not in mgr.load("example.com")
