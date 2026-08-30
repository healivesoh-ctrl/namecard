"""신청 → 서류 업로드 → 반려 → 보완 재제출 → 최종확인 전 과정 테스트.

실행 (care/ 에서):
    ADMIN_PASSWORD=test python -m pytest tests -q
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADMIN = {"X-Admin-Password": "test-pw"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    import server.db as db_mod
    import server.app as app_mod

    importlib.reload(db_mod)
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c


FORM = {
    "mother_name": "김산모",
    "mother_phone": "01012345678",
    "helper_name": "이도우미",
    "helper_phone": "010-9876-5432",
    "helper_relation": "가족",
    "relation_detail": "친정어머니",
    "due_date": "2026-10-15",
    "service_days": 20,
    "voucher_code": "",
    "center_use": "이용함",
    "center_period": "2주",
    "referral": "맘카페·온라인 커뮤니티",
    "consents": {"privacy": True, "sensitive": True, "kakao": True, "marketing": False},
    "kakao_friend": True,
}


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


def test_info_lists_documents_and_fields_before_applying(client):
    """접수 전에 필요한 서류와 입력 항목을 확인할 수 있다."""
    info = client.get("/api/info").json()
    labels = [d["label"] for d in info["documents"]]
    assert labels == [
        "산모신생아건강관리사 교육수료증",
        "아동학대예방교육 수료증",
        "건강검진결과서",
        "백일해 예방접종 증명서류",
    ]
    assert info["service_days_default"] == 15
    assert info["service_days_options"] == [5, 10, 15, 20, 25]
    field_keys = {f["key"] for f in info["fields"]}
    assert {"mother_name", "mother_phone", "helper_name", "helper_phone", "due_date",
            "service_days", "voucher_code"} <= field_keys
    # 보건소 코드는 선택 항목 — 모르면 넘어갈 수 있어야 한다
    assert next(f for f in info["fields"] if f["key"] == "voucher_code")["required"] is False


def test_submit_without_any_document_is_auto_approved(client):
    """서류가 하나도 없어도 접수는 되고 자동 승인된다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    assert app["status"] == "received"
    assert app["status_label"] == "접수완료"
    assert app["code"].startswith("DA-")
    assert app["mother_phone"] == "010-1234-5678"  # 연락처 정규화
    assert len(app["missing_now"]) == 4
    assert any(e["kind"] == "received" for e in app["events"])


def test_missing_required_field_blocks_submit_but_not_draft(client):
    payload = {**FORM, "mother_name": "", "submit": True}
    res = client.post("/api/applications", json=payload)
    assert res.status_code == 400
    assert "산모 이름" in res.json()["detail"]

    draft = client.post("/api/applications", json={**payload, "submit": False}).json()
    assert draft["status"] == "draft"


def test_draft_holds_documents_until_applicant_submits(client):
    """접수하지 않은 채 서류만 저장해두었다가 나중에 접수할 수 있다."""
    draft = client.post("/api/applications", json={**FORM, "submit": False}).json()
    code, token = draft["code"], draft["token"]
    assert draft["status"] == "draft"

    up = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "health_checkup"},
        files={"file": ("검진.pdf", _pdf(), "application/pdf")},
    ).json()
    assert up["status"] == "draft"  # 서류를 올려도 접수되지는 않는다
    assert len(up["documents"]) == 1

    submitted = client.post(f"/api/applications/{code}/submit?token={token}").json()
    assert submitted["status"] == "received"
    assert len(submitted["documents"]) == 1


def test_upload_rejects_bad_extension_and_oversize(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]

    bad = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "pertussis"},
        files={"file": ("악성.exe", b"MZ", "application/octet-stream")},
    )
    assert bad.status_code == 400

    import server.app as app_mod
    huge = b"x" * (app_mod.MAX_UPLOAD_BYTES + 1)
    too_big = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "pertussis"},
        files={"file": ("큰파일.pdf", huge, "application/pdf")},
    )
    assert too_big.status_code == 413

    unknown = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "무엇"},
        files={"file": ("a.pdf", _pdf(), "application/pdf")},
    )
    assert unknown.status_code == 400


def test_wrong_token_cannot_read_or_upload(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code = app["code"]
    assert client.get(f"/api/applications/{code}?token=wrong").status_code == 404
    assert client.get(f"/api/applications/{code}").status_code == 404
    res = client.post(
        f"/api/applications/{code}/documents?token=wrong",
        data={"doc_type": "pertussis"},
        files={"file": ("a.pdf", _pdf(), "application/pdf")},
    )
    assert res.status_code == 404


def test_lookup_by_three_fields(client):
    """접수번호 없이 산모 이름·연락처·산후도우미 이름 세 가지로 찾는다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    found = client.post("/api/applications/lookup", json={
        "mother_name": "김산모", "phone": "010 1234 5678", "helper_name": "이도우미"}).json()
    assert found["token"] == app["token"]
    assert found["code"] == app["code"]


@pytest.mark.parametrize("wrong", [
    {"mother_name": "박산모", "phone": "01012345678", "helper_name": "이도우미"},
    {"mother_name": "김산모", "phone": "01099999999", "helper_name": "이도우미"},
    {"mother_name": "김산모", "phone": "01012345678", "helper_name": "최도우미"},
])
def test_lookup_needs_all_three_to_match(client, wrong):
    client.post("/api/applications", json={**FORM, "submit": True})
    assert client.post("/api/applications/lookup", json=wrong).status_code == 404


def test_lookup_requires_every_field(client):
    res = client.post("/api/applications/lookup",
                      json={"mother_name": "김산모", "phone": "", "helper_name": "이도우미"})
    assert res.status_code == 400
    assert "산후도우미 이름" in res.json()["detail"]


def test_lookup_returns_most_recent_when_applied_twice(client):
    client.post("/api/applications", json={**FORM, "submit": True})
    second = client.post("/api/applications", json={**FORM, "submit": True}).json()
    found = client.post("/api/applications/lookup", json={
        "mother_name": "김산모", "phone": "01012345678", "helper_name": "이도우미"}).json()
    assert found["code"] == second["code"]


def test_reject_then_upload_missing_docs_then_resubmit(client):
    """관리자가 부족 서류를 지정해 반려하면, 신청자는 부족분만 올려 재제출한다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    for doc_type in ("caregiver_cert", "abuse_prevention"):
        client.post(
            f"/api/applications/{code}/documents?token={token}",
            data={"doc_type": doc_type},
            files={"file": (f"{doc_type}.pdf", _pdf(), "application/pdf")},
        )

    rejected = client.post(
        f"/api/admin/applications/{code}/status",
        headers=ADMIN,
        json={
            "status": "rejected",
            "message": "건강검진결과서에 결핵 검사 항목이 필요합니다.",
            "missing_docs": ["health_checkup", "pertussis"],
        },
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["missing_doc_labels"] == ["건강검진결과서", "백일해 예방접종 증명서류"]

    # 신청자 화면에서 반려 사유와 부족 서류가 보인다
    seen = client.get(f"/api/applications/{code}?token={token}").json()
    assert "결핵" in seen["admin_message"]
    assert seen["missing_docs"] == ["health_checkup", "pertussis"]

    for doc_type in ("health_checkup", "pertussis"):
        client.post(
            f"/api/applications/{code}/documents?token={token}",
            data={"doc_type": doc_type},
            files={"file": (f"{doc_type}.jpg", b"\xff\xd8\xff\xe0jpegdata", "image/jpeg")},
        )
    again = client.post(f"/api/applications/{code}/submit?token={token}").json()
    assert again["status"] == "received"
    assert again["missing_docs"] == []
    assert again["admin_message"] == ""
    assert again["missing_now"] == []


def test_reject_requires_reason_or_missing_docs(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    res = client.post(f"/api/admin/applications/{app['code']}/status", headers=ADMIN,
                      json={"status": "rejected", "message": "", "missing_docs": []})
    assert res.status_code == 400


def test_stage_visible_to_applicant_through_confirmation(client):
    """접수 → 검토 → 최종확인 단계가 신청자에게 그대로 보인다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]

    client.post(f"/api/admin/applications/{code}/status", headers=ADMIN,
                json={"status": "reviewing", "message": "서류 확인중입니다."})
    mid = client.get(f"/api/applications/{code}?token={token}").json()
    assert mid["status_label"] == "서류 검토중"
    assert mid["step"] == 2

    client.post(f"/api/admin/applications/{code}/status", headers=ADMIN,
                json={"status": "confirmed", "message": "모든 서류 확인 완료"})
    done = client.get(f"/api/applications/{code}?token={token}").json()
    assert done["status"] == "confirmed"
    assert done["step"] == 3
    assert done["confirmed_at"]

    # 최종확인 후에는 수정·추가 업로드가 막힌다
    assert client.post(f"/api/applications/{code}/submit?token={token}").status_code == 409
    assert client.patch(f"/api/applications/{code}?token={token}", json=FORM).status_code == 409
    blocked = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "pertussis"},
        files={"file": ("a.pdf", _pdf(), "application/pdf")},
    )
    assert blocked.status_code == 409


def test_admin_document_review_and_file_access(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    up = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "caregiver_cert"},
        files={"file": ("수료증.pdf", _pdf(), "application/pdf")},
    ).json()
    doc_id = up["documents"][0]["id"]

    reviewed = client.post(f"/api/admin/documents/{doc_id}/review", headers=ADMIN,
                           json={"status": "rejected", "reason": "이름이 보이지 않습니다."}).json()
    assert reviewed["documents"][0]["status"] == "rejected"
    # 반려된 서류는 '제출됨'으로 치지 않는다
    assert "산모신생아건강관리사 교육수료증" in reviewed["missing_now"]

    # 신청자·관리자 모두 파일을 열 수 있고, 캐시에 남지 않는다
    f1 = client.get(f"/api/applications/{code}/documents/{doc_id}/file?token={token}")
    assert f1.status_code == 200 and f1.content == _pdf()
    assert "no-store" in f1.headers["cache-control"]
    f2 = client.get(f"/api/admin/applications/{code}/documents/{doc_id}/file", headers=ADMIN)
    assert f2.status_code == 200

    accepted = client.post(f"/api/admin/documents/{doc_id}/review", headers=ADMIN,
                           json={"status": "accepted"}).json()
    assert accepted["missing_now"] == [
        "아동학대예방교육 수료증", "건강검진결과서", "백일해 예방접종 증명서류",
    ]


def test_document_delete(client):
    app = client.post("/api/applications", json={**FORM, "submit": False}).json()
    code, token = app["code"], app["token"]
    up = client.post(
        f"/api/applications/{code}/documents?token={token}",
        data={"doc_type": "pertussis"},
        files={"file": ("접종.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    ).json()
    doc_id = up["documents"][0]["id"]
    after = client.request("DELETE", f"/api/applications/{code}/documents/{doc_id}?token={token}").json()
    assert after["documents"] == []


def test_admin_requires_password(client):
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    assert client.get("/api/admin/applications").status_code == 401
    assert client.get("/api/admin/applications", headers={"X-Admin-Password": "nope"}).status_code == 401
    assert client.get(f"/api/admin/applications/{app['code']}",
                      headers={"X-Admin-Password": "nope"}).status_code == 401


def test_admin_list_filters_and_counts(client):
    client.post("/api/applications", json={**FORM, "submit": True})
    client.post("/api/applications", json={**FORM, "mother_name": "박산모", "submit": False})
    listed = client.get("/api/admin/applications", headers=ADMIN).json()
    assert listed["counts"]["received"] == 1 and listed["counts"]["draft"] == 1

    only_draft = client.get("/api/admin/applications?status=draft", headers=ADMIN).json()
    assert [i["mother_name"] for i in only_draft["items"]] == ["박산모"]

    searched = client.get("/api/admin/applications?q=박산모", headers=ADMIN).json()
    assert len(searched["items"]) == 1


def test_pay_settings_shown_on_main(client):
    """관리자가 친정엄마 소득을 바꾸면 메인 안내(/api/info)에 그대로 반영된다."""
    assert client.get("/api/info").json()["pay"]["rows"] == []

    saved = client.put("/api/admin/pay", headers=ADMIN, json={
        "effective_month": "2026년 9월",
        "hourly_wage": 10320,
        "headline": "9월 친정엄마 예상 소득",
        "note": "이벤트 기간 한정",
        "rows": [
            {"label": "15일 이용", "amount": "1,650,000", "note": "기본"},
            {"label": "", "amount": 100},          # 라벨 없는 행은 버려진다
            {"label": "25일 이상", "amount": "잘못된값"},
        ],
    }).json()
    assert [r["label"] for r in saved["rows"]] == ["15일 이용", "25일 이상"]
    assert saved["rows"][0]["amount"] == 1650000
    assert saved["rows"][1]["amount"] == 0

    pay = client.get("/api/info").json()["pay"]
    assert pay["headline"] == "9월 친정엄마 예상 소득"
    assert pay["hourly_wage"] == 10320
    assert pay["updated_at"]


def test_consultation_request(client):
    res = client.post("/api/consultations", json={"name": "홍길동", "phone": "01011112222",
                                                  "preferred_time": "평일 오후", "memo": "문의드립니다"}).json()
    assert res["ok"] is True
    listed = client.get("/api/admin/consultations", headers=ADMIN).json()
    assert listed["items"][0]["phone"] == "010-1111-2222"
    assert listed["items"][0]["status"] == "new"

    cid = listed["items"][0]["id"]
    client.post(f"/api/admin/consultations/{cid}", headers=ADMIN, json={"status": "done", "admin_note": "통화완료"})
    assert client.get("/api/admin/consultations", headers=ADMIN).json()["items"][0]["status"] == "done"

    assert client.post("/api/consultations", json={"name": "홍길동", "phone": "없음"}).status_code == 400


def test_patch_updates_fields_including_late_voucher_code(client):
    """보건소 코드를 몰라 비워둔 뒤 나중에 채워 넣을 수 있다."""
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    assert app["voucher_code"] == ""

    updated = client.patch(f"/api/applications/{code}?token={token}",
                           json={**FORM, "voucher_code": "SEOUL-2026-0001", "service_days": 25}).json()
    assert updated["voucher_code"] == "SEOUL-2026-0001"
    assert updated["service_days"] == 25


def test_invalid_input_normalization(client):
    app = client.post("/api/applications",
                      json={**FORM, "service_days": 7, "helper_relation": "이상한값", "submit": False}).json()
    assert app["service_days"] == 15   # 허용 목록 밖이면 기본값
    assert app["helper_relation"] == "가족"

    bad_date = client.post("/api/applications", json={**FORM, "due_date": "2026/10/15"})
    assert bad_date.status_code == 400


def test_pages_are_served(client):
    for path in ("/", "/apply.html", "/status.html", "/admin.html", "/app.css", "/app.js"):
        assert client.get(path).status_code == 200, path


def test_due_date_month_only_is_allowed(client):
    """일자가 미정이면 연·월까지만 받아도 접수된다."""
    app = client.post("/api/applications", json={**FORM, "due_date": "2026-10", "submit": True}).json()
    assert app["due_date"] == "2026-10"

    exact = client.post("/api/applications", json={**FORM, "due_date": "2026-10-15", "submit": True}).json()
    assert exact["due_date"] == "2026-10-15"

    bad = client.post("/api/applications", json={**FORM, "due_date": "2026", "submit": True})
    assert bad.status_code == 400
    assert "연·월" in bad.json()["detail"]


def test_postpartum_center_fields(client):
    app = client.post("/api/applications",
                      json={**FORM, "center_use": "이용함", "center_period": "4주 이상", "submit": True}).json()
    assert app["center_use"] == "이용함" and app["center_period"] == "4주 이상"

    # 이용하지 않으면 기간은 남기지 않는다
    none = client.post("/api/applications",
                       json={**FORM, "center_use": "이용 안 함", "center_period": "2주", "submit": True}).json()
    assert none["center_period"] == ""

    assert client.post("/api/applications",
                       json={**FORM, "center_use": "가끔", "submit": True}).status_code == 400
    assert client.post("/api/applications",
                       json={**FORM, "center_period": "6주", "submit": True}).status_code == 400


def test_center_use_is_required_to_submit(client):
    res = client.post("/api/applications", json={**FORM, "center_use": "", "submit": True})
    assert res.status_code == 400 and "산후조리원 이용 여부" in res.json()["detail"]
    draft = client.post("/api/applications", json={**FORM, "center_use": "", "submit": False}).json()
    assert draft["status"] == "draft"


def test_voucher_code_guidance_is_public(client):
    info = client.get("/api/info").json()
    assert info["voucher_code_example"] == "A-통합-1형"
    assert "복지로" in info["voucher_code_help"] and "보건소" in info["voucher_code_help"]
    field = next(f for f in info["fields"] if f["key"] == "voucher_code")
    assert field["label"] == "바우처 구분코드" and field["required"] is False
    assert info["center_period_options"] == ["1주", "2주", "3주", "4주 이상", "미정"]


def test_referral_is_optional_and_recorded(client):
    """인지경로는 선택 항목이지만, 고르면 그대로 저장된다."""
    info = client.get("/api/info").json()
    assert "맘카페·온라인 커뮤니티" in info["referral_options"]
    assert "지인 소개" in info["referral_options"]
    assert next(f for f in info["fields"] if f["key"] == "referral")["required"] is False

    app = client.post("/api/applications", json={**FORM, "referral": "지인 소개", "submit": True}).json()
    assert app["referral"] == "지인 소개"

    blank = client.post("/api/applications", json={**FORM, "referral": "", "submit": True}).json()
    assert blank["referral"] == ""      # 답하지 않아도 접수된다

    bad = client.post("/api/applications", json={**FORM, "referral": "텔레파시", "submit": True})
    assert bad.status_code == 400


def test_referral_detail_only_kept_for_other(client):
    """자유 입력은 '기타'를 골랐을 때만 남긴다."""
    other = client.post("/api/applications", json={
        **FORM, "referral": "기타", "referral_detail": "산부인과 대기실 포스터", "submit": True}).json()
    assert other["referral_detail"] == "산부인과 대기실 포스터"

    named = client.post("/api/applications", json={
        **FORM, "referral": "유튜브", "referral_detail": "엉뚱한 값", "submit": True}).json()
    assert named["referral_detail"] == ""


def test_admin_sees_referral_breakdown(client):
    """어디에 홍보를 더 할지 판단할 수 있게 경로별 건수를 집계한다."""
    for r in ["지인 소개", "지인 소개", "인터넷 검색", ""]:
        client.post("/api/applications", json={**FORM, "referral": r, "submit": True})
    referrals = client.get("/api/admin/applications", headers=ADMIN).json()["referrals"]
    counts = {r["label"]: r["count"] for r in referrals}
    assert counts["지인 소개"] == 2
    assert counts["인터넷 검색"] == 1
    assert counts["응답 없음"] == 1
    assert referrals[0]["label"] == "지인 소개"      # 많은 순
