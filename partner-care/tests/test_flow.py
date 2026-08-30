"""접수 → 양방향 인증 → 수령 전 과정과 우회 시도(양도·단독승인)에 대한 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SERVER_SECRET", "test-secret")
    monkeypatch.setenv("OTP_DEBUG", "1")
    monkeypatch.setenv("DAIEUM_ADMIN_PASSWORD", "daieum-test")
    monkeypatch.setenv("PARTNER_ADMIN_PASSWORD__DAECHUBAT", "store-test")
    monkeypatch.setenv("PARTNER_CONFIG", str(BASE / "config" / "partners.example.json"))

    from app import config, main  # noqa: WPS433

    config.load(force=True)
    return TestClient(main.app)


APPLICANT = {
    "partner_id": "daechubat",
    "service_id": "postpartum-helper",
    "product_id": "postpartum-herb-30",
    "name": "김산모",
    "phone": "010-1234-5678",
    "identity": {"chart_no": "20451", "last_visit": "2026-08-01", "patient_name_match": "김산모"},
    "booking": {"due_date": "2026-09-20", "period": "10일", "start_date": "2026-09-25",
                "address": "대전 유성구 봉명동"},
    "agree_terms": True,
    "agree_privacy": True,
    "agree_no_transfer": True,
}


def _apply(client, **over):
    body = {**APPLICANT, **over}
    res = client.post("/api/claims", json=body)
    assert res.status_code == 201, res.text
    data = res.json()
    verify = client.post(f"/api/claims/{data['claim_id']}/otp/verify",
                         json={"code": data["dev_code"]})
    assert verify.status_code == 200, verify.text
    return data["claim_id"], verify.json()["token"]


def _login(client, role, password, partner_id=None):
    res = client.post("/api/admin/login",
                      json={"role": role, "partner_id": partner_id, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["token"]


# ── 카탈로그 / 커스터마이징 ────────────────────────────────
def test_catalog_exposes_partner_without_secrets(client):
    cat = client.get("/api/catalog").json()
    partner = cat["partners"][0]
    assert partner["name"] == "대추밭한의원"
    assert "admin_password_sha256" not in partner
    assert [f["key"] for f in partner["identity_fields"]] == [
        "chart_no", "last_visit", "patient_name_match"
    ]
    assert {p["id"] for p in partner["products"]} == {"postpartum-herb-30", "warm-care-set"}


def test_identity_fields_are_enforced_per_partner(client):
    res = client.post("/api/claims", json={**APPLICANT, "identity": {"chart_no": "abc"}})
    assert res.status_code == 400
    assert "진료(차트)번호" in res.json()["detail"]


# ── 본인확인 ──────────────────────────────────────────────
def test_phone_verification_required_before_review(client):
    res = client.post("/api/claims", json=APPLICANT)
    claim_id = res.json()["claim_id"]
    bad = client.post(f"/api/claims/{claim_id}/otp/verify", json={"code": "000000"})
    assert bad.status_code == 400

    store_token = _login(client, "store", "store-test", "daechubat")
    approve = client.post(f"/api/admin/claims/{claim_id}/approve",
                          headers={"X-Admin-Token": store_token},
                          json={"checks": ["chart_no", "last_visit", "patient_name_match"]})
    assert approve.status_code == 409  # 본인확인 전에는 승인 불가


# ── 양방향 인증 ───────────────────────────────────────────
def test_single_approval_does_not_release_code(client):
    claim_id, token = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    res = client.post(f"/api/admin/claims/{claim_id}/approve",
                      headers={"X-Admin-Token": store_token},
                      json={"checks": ["chart_no", "last_visit", "patient_name_match"]})
    assert res.json()["dual_approved"] is False
    assert "디에이블 다이음" in res.json()["waiting_for"]

    code = client.get(f"/api/claims/{claim_id}/release-code", headers={"X-Claim-Token": token})
    assert code.status_code == 409
    assert "디에이블 다이음" in code.json()["detail"]


def test_store_must_check_identity_items_before_approving(client):
    claim_id, _ = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    res = client.post(f"/api/admin/claims/{claim_id}/approve",
                      headers={"X-Admin-Token": store_token}, json={"checks": ["chart_no"]})
    assert res.status_code == 400
    assert "미확인" in res.json()["detail"]


def test_dual_approval_then_fulfillment(client):
    claim_id, token = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    op_token = _login(client, "operator", "daieum-test")

    client.post(f"/api/admin/claims/{claim_id}/approve",
                headers={"X-Admin-Token": store_token},
                json={"checks": ["chart_no", "last_visit", "patient_name_match"],
                      "note": "차트 대조 완료"})
    second = client.post(f"/api/admin/claims/{claim_id}/approve",
                         headers={"X-Admin-Token": op_token}, json={"note": "배정 가능"})
    assert second.json()["dual_approved"] is True

    code_res = client.get(f"/api/claims/{claim_id}/release-code",
                          headers={"X-Claim-Token": token})
    assert code_res.status_code == 200
    code = code_res.json()["code"]

    wrong = client.post(f"/api/admin/claims/{claim_id}/fulfill",
                        headers={"X-Admin-Token": op_token},
                        json={"code": code, "phone_last4": "9999"})
    assert wrong.status_code == 400  # 본인 휴대폰 뒷자리 불일치

    ok = client.post(f"/api/admin/claims/{claim_id}/fulfill",
                     headers={"X-Admin-Token": op_token},
                     json={"code": code, "phone_last4": "5678", "note": "택배 발송"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["receipt"]

    again = client.post(f"/api/admin/claims/{claim_id}/fulfill",
                        headers={"X-Admin-Token": op_token},
                        json={"code": code, "phone_last4": "5678"})
    assert again.status_code == 409  # 1회용


def test_wrong_release_code_is_rejected(client):
    claim_id, _ = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    op_token = _login(client, "operator", "daieum-test")
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": store_token},
                json={"checks": ["chart_no", "last_visit", "patient_name_match"]})
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": op_token})
    res = client.post(f"/api/admin/claims/{claim_id}/fulfill",
                      headers={"X-Admin-Token": op_token},
                      json={"code": "000000", "phone_last4": "5678"})
    assert res.status_code == 400


# ── 양도 방지 ─────────────────────────────────────────────
def test_identity_change_invalidates_approvals(client):
    """접수 내용이 바뀌면 지문이 달라져 이전 승인이 자동 무효화된다."""
    from app import store as claim_store

    claim_id, token = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    op_token = _login(client, "operator", "daieum-test")
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": store_token},
                json={"checks": ["chart_no", "last_visit", "patient_name_match"]})
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": op_token})
    assert client.get(f"/api/claims/{claim_id}/release-code",
                      headers={"X-Claim-Token": token}).status_code == 200

    # 저장소를 직접 조작해 '지인에게 넘기기'를 시도
    claim = claim_store.get(claim_id)
    claim.applicant_name = "이지인"
    claim_store.save(claim)

    blocked = client.get(f"/api/claims/{claim_id}/release-code", headers={"X-Claim-Token": token})
    assert blocked.status_code == 409
    detail = client.get(f"/api/admin/claims/{claim_id}",
                        headers={"X-Admin-Token": op_token}).json()
    assert detail["status"] == "pending"
    assert detail["approvals"]["store"]["valid"] is False


def test_phone_change_invalidates_applicant_token(client):
    from app import security
    from app import store as claim_store

    claim_id, token = _apply(client)
    claim = claim_store.get(claim_id)
    claim.phone_key = security.phone_key("010-9999-0000")
    claim_store.save(claim)
    res = client.get(f"/api/claims/{claim_id}", headers={"X-Claim-Token": token})
    assert res.status_code == 401


def test_one_claim_per_person(client):
    _apply(client)
    res = client.post("/api/claims", json=APPLICANT)
    assert res.status_code == 409
    assert "1인" in res.json()["detail"]


def test_cancelled_claim_frees_the_quota(client):
    claim_id, token = _apply(client)
    client.post(f"/api/claims/{claim_id}/cancel", headers={"X-Claim-Token": token})
    res = client.post("/api/claims", json=APPLICANT)
    assert res.status_code == 201


# ── 권한 분리 ─────────────────────────────────────────────
def test_store_admin_cannot_see_other_partner_claims(client, monkeypatch):
    from app import config as cfg

    data = cfg.load()
    other = dict(data["partners"][0], id="other-shop", name="다른가게")
    data["partners"].append(other)
    monkeypatch.setenv("PARTNER_ADMIN_PASSWORD__OTHER_SHOP", "other-test")

    claim_id, _ = _apply(client)
    other_token = _login(client, "store", "other-test", "other-shop")
    listing = client.get("/api/admin/claims", headers={"X-Admin-Token": other_token}).json()
    assert listing["claims"] == []
    res = client.get(f"/api/admin/claims/{claim_id}", headers={"X-Admin-Token": other_token})
    assert res.status_code == 403


def test_operator_cannot_read_store_identity_fields(client):
    claim_id, _ = _apply(client)
    op_token = _login(client, "operator", "daieum-test")
    view = client.get(f"/api/admin/claims/{claim_id}", headers={"X-Admin-Token": op_token}).json()
    assert "20451" not in str(view["identity"])
    store_token = _login(client, "store", "store-test", "daechubat")
    store_view = client.get(f"/api/admin/claims/{claim_id}",
                            headers={"X-Admin-Token": store_token}).json()
    assert store_view["identity"]["chart_no"] == "20451"


def test_admin_endpoints_require_login(client):
    claim_id, _ = _apply(client)
    assert client.get("/api/admin/claims").status_code == 401
    assert client.post(f"/api/admin/claims/{claim_id}/approve").status_code == 401
    assert client.post("/api/admin/login", json={"role": "store", "partner_id": "daechubat",
                                                 "password": "틀린비밀번호"}).status_code == 401


def test_audit_trail_records_both_approvals(client):
    claim_id, _ = _apply(client)
    store_token = _login(client, "store", "store-test", "daechubat")
    op_token = _login(client, "operator", "daieum-test")
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": store_token},
                json={"checks": ["chart_no", "last_visit", "patient_name_match"]})
    client.post(f"/api/admin/claims/{claim_id}/approve", headers={"X-Admin-Token": op_token})
    trail = client.get(f"/api/admin/claims/{claim_id}/audit",
                       headers={"X-Admin-Token": op_token}).json()["trail"]
    events = [row["event"] for row in trail]
    assert "created" in events and "phone_verified" in events
    assert "approved:store" in events and "approved:operator" in events
