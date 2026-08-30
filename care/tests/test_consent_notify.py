"""개인정보 동의 · 카카오채널 친구추가 · 알림톡 자동발송 테스트."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADMIN = {"X-Admin-Password": "test-pw"}
CONSENTS = {"privacy": True, "sensitive": True, "kakao": True, "marketing": False}
FORM = {
    "mother_name": "김서연", "mother_phone": "01023457788",
    "helper_name": "이복순", "helper_phone": "01034561234",
    "helper_relation": "가족", "relation_detail": "친정어머니",
    "due_date": "2026-09-20", "service_days": 15, "voucher_code": "",
    "center_use": "이용함",
    "center_period": "2주",
    "consents": CONSENTS, "kakao_friend": True,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.delenv("ALIMTALK_MODE", raising=False)
    monkeypatch.delenv("ALIMTALK_WEBHOOK_URL", raising=False)
    import server.db as db_mod
    import server.gsync as gsync_mod
    import server.notify as notify_mod
    import server.app as app_mod
    for m in (db_mod, gsync_mod, notify_mod, app_mod):
        importlib.reload(m)
    with TestClient(app_mod.app) as c:
        yield c


def _notify():
    import server.notify as n
    assert n.flush(timeout=10)
    return n


# ── 개인정보 동의 ────────────────────────────────────────────

def test_info_exposes_consent_texts_before_applying(client):
    """접수 전에 무엇에 동의하게 되는지 미리 볼 수 있어야 한다."""
    info = client.get("/api/info").json()
    keys = [c["key"] for c in info["consents"]]
    assert keys == ["privacy", "sensitive", "kakao", "marketing"]
    privacy = info["consents"][0]
    assert privacy["required"] is True
    assert "산모 이름" in privacy["items"]
    assert privacy["period"]
    assert next(c for c in info["consents"] if c["key"] == "marketing")["required"] is False
    assert info["consent_version"]


@pytest.mark.parametrize("missing_key", ["privacy", "sensitive", "kakao"])
def test_submit_blocked_without_each_required_consent(client, missing_key):
    payload = {**FORM, "consents": {**CONSENTS, missing_key: False}, "submit": True}
    res = client.post("/api/applications", json=payload)
    assert res.status_code == 400
    label = {"privacy": "개인정보 수집·이용", "sensitive": "민감정보", "kakao": "알림톡"}[missing_key]
    assert label in res.json()["detail"]


def test_marketing_consent_is_optional(client):
    res = client.post("/api/applications",
                      json={**FORM, "consents": {**CONSENTS, "marketing": False}, "submit": True})
    assert res.status_code == 200
    assert res.json()["consents"]["marketing"] is False


def test_draft_can_be_saved_without_consent(client):
    """임시저장은 동의 없이도 된다 — 접수할 때만 막는다."""
    draft = client.post("/api/applications",
                        json={**FORM, "consents": {}, "kakao_friend": False, "submit": False}).json()
    assert draft["status"] == "draft"
    assert draft["consents"]["privacy"] is False

    res = client.post(f"/api/applications/{draft['code']}/submit?token={draft['token']}")
    assert res.status_code == 400 and "동의" in res.json()["detail"]


def test_consent_records_time_and_version(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    assert app["consent_at"], "동의 시각이 기록되어야 한다"
    import server.db as db_mod
    assert app["consent_version"] == db_mod.CONSENT_VERSION


# ── 카카오채널 친구추가 ──────────────────────────────────────

def test_submit_blocked_without_kakao_friend(client):
    res = client.post("/api/applications", json={**FORM, "kakao_friend": False, "submit": True})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "친구추가" in detail and "이벤트" in detail


def test_friend_add_turns_on_event_by_default(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    assert app["kakao_friend"] is True
    assert app["event_applied"] is True


def test_admin_can_mark_friend_removed_and_drop_event(client):
    """친구를 삭제한 것으로 확인되면 이벤트 대상에서 빠진다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    updated = client.post(f"/api/admin/applications/{app['code']}/kakao", headers=ADMIN,
                          json={"verified": "no", "event_applied": True}).json()
    assert updated["kakao_verified"] == "no"
    assert updated["event_applied"] is False
    assert any("이벤트 미적용" in e["message"] for e in updated["events"])

    back = client.post(f"/api/admin/applications/{app['code']}/kakao", headers=ADMIN,
                       json={"verified": "yes", "event_applied": True}).json()
    assert back["event_applied"] is True


# ── 알림톡 ──────────────────────────────────────────────────

def test_alimtalk_queued_on_submit(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    _notify()
    items = client.get("/api/admin/notifications", headers=ADMIN).json()["items"]
    assert len(items) == 1
    n = items[0]
    assert n["template_key"] == "received"
    assert n["phone"] == "010-2345-7788"
    assert "김서연" in n["body"] and app["code"] in n["body"]
    assert "산모신생아건강관리사 교육수료증" in n["body"]   # 아직 안 낸 서류를 알려준다
    # 채널은 "삭제하면 못 받는다"가 아니라 "진행 상황을 보는 창구"로 안내한다
    assert "카카오톡에서 바로 확인" in n["body"]
    assert "채널을 그대로 두시면" in n["body"]
    assert "1년" not in n["body"]
    assert "삭제" not in n["body"]


def test_alimtalk_sent_at_every_stage(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code = app["code"]
    for status, message in [("reviewing", "확인중입니다"), ("confirmed", "모두 확인했습니다")]:
        client.post(f"/api/admin/applications/{code}/status", headers=ADMIN,
                    json={"status": status, "message": message})
    _notify()
    keys = [n["template_key"] for n in
            client.get(f"/api/admin/notifications?code={code}", headers=ADMIN).json()["items"]]
    assert keys == ["confirmed", "reviewing", "received"]   # 최신순


def test_rejection_alimtalk_carries_reason_and_missing_docs(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    client.post(f"/api/admin/applications/{app['code']}/status", headers=ADMIN, json={
        "status": "rejected", "message": "결핵 항목이 포함된 결과서로 다시 올려주세요.",
        "missing_docs": ["health_checkup", "pertussis"]})
    _notify()
    body = client.get(f"/api/admin/notifications?code={app['code']}",
                      headers=ADMIN).json()["items"][0]["body"]
    assert "건강검진결과서, 백일해 예방접종 증명서류" in body
    assert "결핵 항목이 포함된 결과서로 다시 올려주세요." in body
    assert "처음부터 다시 작성하실 필요는 없습니다" in body


def test_consultation_alimtalk(client):
    client.post("/api/consultations", json={"name": "이하나", "phone": "01012340000",
                                            "preferred_time": "평일 오후"})
    _notify()
    items = client.get("/api/admin/notifications", headers=ADMIN).json()["items"]
    assert items[0]["template_key"] == "consult"
    assert "이하나" in items[0]["body"] and "평일 오후" in items[0]["body"]


def test_dryrun_records_but_does_not_send(client):
    client.post("/api/applications", json={**FORM, "submit": True})
    _notify()
    data = client.get("/api/admin/notifications", headers=ADMIN).json()
    assert data["mode"] == "dryrun" and data["live"] is False
    assert data["items"][0]["status"] == "skipped"
    assert "dryrun" in data["items"][0]["detail"]


def test_webhook_mode_sends(client, monkeypatch):
    """업체 연동은 웹훅 하나로 붙인다 — 실제 발송 경로를 검증한다."""
    import server.notify as n
    monkeypatch.setenv("ALIMTALK_MODE", "webhook")
    monkeypatch.setenv("ALIMTALK_WEBHOOK_URL", "https://example.test/send")
    monkeypatch.setenv("ALIMTALK_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ALIMTALK_TEMPLATE_IDS", json.dumps({"received": "TPL_RECV_01"}))

    calls = []

    class FakeRes:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeRes()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    client.post("/api/applications", json={**FORM, "submit": True})
    assert n.flush(timeout=10)

    assert len(calls) == 1
    sent = calls[0]
    assert sent["url"] == "https://example.test/send"
    assert sent["headers"]["X-Webhook-Secret"] == "s3cret"
    assert sent["json"]["template_id"] == "TPL_RECV_01"
    assert sent["json"]["to"] == "010-2345-7788"
    assert "김서연" in sent["json"]["text"]

    data = client.get("/api/admin/notifications", headers=ADMIN).json()
    assert data["live"] is True
    assert data["items"][0]["status"] == "sent" and data["items"][0]["sent_at"]


def test_send_failure_is_recorded_and_resendable(client, monkeypatch):
    import requests
    import server.notify as n
    monkeypatch.setenv("ALIMTALK_MODE", "webhook")
    monkeypatch.setenv("ALIMTALK_WEBHOOK_URL", "https://example.test/send")

    class Bad:
        status_code = 500
        text = "서버 오류"

    monkeypatch.setattr(requests, "post", lambda *a, **k: Bad())
    app = client.post("/api/applications", json={**FORM, "submit": True})
    assert app.status_code == 200, "발송이 실패해도 접수는 성공해야 한다"
    assert n.flush(timeout=10)

    item = client.get("/api/admin/notifications", headers=ADMIN).json()["items"][0]
    assert item["status"] == "failed" and "500" in item["detail"]

    class Good:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(requests, "post", lambda *a, **k: Good())
    assert client.post(f"/api/admin/notifications/{item['id']}/resend", headers=ADMIN).status_code == 200
    assert n.flush(timeout=10)
    assert client.get("/api/admin/notifications", headers=ADMIN).json()["items"][0]["status"] == "sent"


def test_templates_are_listed_for_kakao_approval(client):
    """카카오 템플릿 심사에 올릴 문구를 관리자 화면에서 그대로 확인할 수 있어야 한다."""
    data = client.get("/api/admin/notifications", headers=ADMIN).json()
    keys = {t["key"] for t in data["templates"]}
    assert keys == {"received", "reviewing", "rejected", "confirmed", "consult"}
    for t in data["templates"]:
        assert t["body"].startswith("[다이음 다이렉트]") or "[다이음 다이렉트]" in t["body"]
        assert t["when"]


# ── 브랜드 설정 ──────────────────────────────────────────────

def test_brand_defaults_and_update(client):
    info = client.get("/api/info").json()["brand"]
    assert info["service_name"] == "다이음 다이렉트"

    saved = client.put("/api/admin/brand", headers=ADMIN, json={
        "service_name": "다이음 다이렉트", "tagline": "직접 신청하고 급여는 더 높게",
        "channel_name": "다이음", "channel_url": "https://pf.kakao.com/_example",
        "event_notice": "친구 삭제 시 이벤트가 적용되지 않습니다."}).json()
    assert saved["channel_url"] == "https://pf.kakao.com/_example"
    assert client.get("/api/info").json()["brand"]["tagline"] == "직접 신청하고 급여는 더 높게"

    bad = client.put("/api/admin/brand", headers=ADMIN,
                     json={"channel_url": "pf.kakao.com/_example"})
    assert bad.status_code == 400


def test_notifications_require_admin(client):
    assert client.get("/api/admin/notifications").status_code == 401
    assert client.put("/api/admin/brand", json={}).status_code == 401


# ── 카카오톡에서 진행 조회 (알림톡 버튼) ────────────────────

def test_status_link_button_is_sent_when_public_url_set(client, monkeypatch):
    """접수 후 카톡 메시지의 버튼으로 바로 조회 화면이 열려야 한다."""
    import requests
    import server.notify as n
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://care.example.kr/")
    monkeypatch.setenv("ALIMTALK_MODE", "webhook")
    monkeypatch.setenv("ALIMTALK_WEBHOOK_URL", "https://example.test/send")

    calls = []

    class Res:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, headers=None, timeout=None: (calls.append(json), Res())[1])

    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    assert n.flush(timeout=10)

    button = calls[0]["button"]
    assert button["name"] == "진행상황 확인하기"
    assert button["type"] == "WL"
    assert button["url"] == (
        "https://care.example.kr/status.html?code=" + app["code"] + "&token=" + app["token"])


def test_no_button_without_public_url(client, monkeypatch):
    """주소가 설정되지 않았으면 깨진 버튼을 붙이지 않는다."""
    import requests
    import server.notify as n
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ALIMTALK_MODE", "webhook")
    monkeypatch.setenv("ALIMTALK_WEBHOOK_URL", "https://example.test/send")

    calls = []

    class Res:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, headers=None, timeout=None: (calls.append(json), Res())[1])
    client.post("/api/applications", json={**FORM, "submit": True})
    assert n.flush(timeout=10)
    assert "button" not in calls[0]


def test_rejection_button_points_at_document_upload(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://care.example.kr")
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    client.post(f"/api/admin/applications/{app['code']}/status", headers=ADMIN, json={
        "status": "rejected", "message": "서류를 보완해 주세요.", "missing_docs": ["pertussis"]})
    _notify()
    item = client.get(f"/api/admin/notifications?code={app['code']}", headers=ADMIN).json()["items"][0]
    assert item["link"].startswith("https://care.example.kr/status.html?code=")
    assert "아래 버튼에서 부족한 서류만" in item["body"]


def test_every_stage_template_has_a_button(client):
    """모든 단계 메시지에서 카톡으로 진행 상황을 열 수 있어야 한다."""
    data = client.get("/api/admin/notifications", headers=ADMIN).json()
    for t in data["templates"]:
        if t["key"] == "consult":
            continue          # 전화상담은 접수번호가 없어 조회 대상이 아니다
        assert t["button"], f"{t['key']} 에 버튼이 없습니다"


def test_confirmed_message_invites_keeping_the_channel(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    client.post(f"/api/admin/applications/{app['code']}/status", headers=ADMIN,
                json={"status": "confirmed", "message": "확인 완료"})
    _notify()
    body = client.get(f"/api/admin/notifications?code={app['code']}",
                      headers=ADMIN).json()["items"][0]["body"]
    assert "채널은 그대로 두시면" in body
    assert "삭제" not in body and "미적용" not in body
