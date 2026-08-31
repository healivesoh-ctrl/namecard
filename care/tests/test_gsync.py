"""구글 시트·드라이브 미러링 테스트.

실제 구글에 붙지 않고, HTTP 단일 진입점(gsync._request)을 가짜 스프레드시트로
바꿔치기해서 "어떤 행이 어디에 쓰이는지"를 검증한다.
"""

from __future__ import annotations

import importlib
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADMIN = {"X-Admin-Password": "test-pw"}
FORM = {
    "mother_name": "김산모", "mother_phone": "01012345678",
    "helper_name": "이도우미", "helper_phone": "01098765432",
    "helper_relation": "가족", "relation_detail": "친정어머니",
    "due_date": "2026-10-15", "service_days": 15, "voucher_code": "",
    "center_use": "이용함",
    "center_period": "2주",
    "referral": "맘카페·온라인 커뮤니티", "verify_time": "평일 오후 (12~18시)",
    "consents": {"privacy": True, "sensitive": True, "kakao": True, "marketing": False},
    "kakao_friend": True,
}
PDF = b"%PDF-1.4\ntest\n%%EOF"


class FakeGoogle:
    """시트 탭과 드라이브 업로드를 흉내내는 최소 구현."""

    def __init__(self):
        self.tabs: dict[str, list[list]] = {}
        self.uploads: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_times = 0

    # gsync._request 와 시그니처가 같아야 한다
    def __call__(self, method, url, *, params=None, json_body=None, data=None, headers=None):
        self.calls.append((method, url))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("구글 API 오류 503: 일시적 장애")

        if url.startswith("https://www.googleapis.com/upload/drive/v3/files"):
            self.uploads.append({"body": data, "params": params})
            fid = f"file{len(self.uploads)}"
            return {"id": fid, "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}

        if url.endswith(":batchUpdate"):
            for req in (json_body or {}).get("requests", []):
                title = req["addSheet"]["properties"]["title"]
                self.tabs.setdefault(title, [])
            return {}

        if "/values/" not in url:  # 스프레드시트 메타 조회
            return {"sheets": [{"properties": {"title": t}} for t in self.tabs]}

        raw = url.split("/values/", 1)[1]
        append = raw.endswith(":append")
        if append:
            raw = raw[: -len(":append")]
        ref = urllib.parse.unquote(raw)
        title, a1 = ref.split("!", 1)
        title = title.strip("'")
        rows = self.tabs.setdefault(title, [])

        if method == "GET":
            if a1 == "A1:A1":
                return {"values": [rows[0]]} if rows else {}
            return {"values": [[r[0] if r else ""] for r in rows]}

        values = (json_body or {}).get("values", [])
        if append or a1 == "A1" and not rows:
            for v in values:
                if a1 == "A1" and not rows:
                    rows.append(v)      # 헤더 기록
                else:
                    rows.append(v)
            return {}
        index = int(a1[1:]) - 1 if len(a1) > 1 else 0
        while len(rows) <= index:
            rows.append([])
        rows[index] = values[0]
        return {}

    def rows(self, title: str) -> list[list]:
        """헤더를 뺀 데이터 행."""
        return self.tabs.get(title, [])[1:]

    def row_for(self, title: str, key: str) -> list | None:
        return next((r for r in self.rows(title) if r and r[0] == key), None)


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON",
                       json.dumps({"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"}))
    monkeypatch.setenv("GSHEET_ID", "sheet-abc")
    monkeypatch.setenv("GDRIVE_FOLDER_ID", "folder-xyz")

    import server.db as db_mod
    import server.gsync as gsync_mod
    import server.app as app_mod

    importlib.reload(db_mod)
    importlib.reload(gsync_mod)
    importlib.reload(app_mod)

    fake = FakeGoogle()
    monkeypatch.setattr(gsync_mod, "_request", fake)
    with TestClient(app_mod.app) as c:
        yield c, fake, gsync_mod


def _wait(gsync_mod):
    assert gsync_mod.flush(timeout=15), "동기화 큐가 비워지지 않았습니다."


def test_disabled_without_env(tmp_path, monkeypatch):
    """환경변수가 없으면 연동은 꺼진 채로 서비스가 정상 동작한다."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GSHEET_ID", raising=False)

    import server.db as db_mod
    import server.gsync as gsync_mod
    import server.app as app_mod
    importlib.reload(db_mod); importlib.reload(gsync_mod); importlib.reload(app_mod)

    assert gsync_mod.enabled() is False
    with TestClient(app_mod.app) as c:
        app = c.post("/api/applications", json={**FORM, "submit": True})
        assert app.status_code == 200
        st = c.get("/api/admin/sync", headers=ADMIN).json()
        assert st["enabled"] is False and st["pending"] == 0
        assert c.post("/api/admin/sync", headers=ADMIN).status_code == 400


def test_application_creates_sheet_row(setup):
    client, fake, gsync = setup
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    _wait(gsync)

    assert fake.tabs["신청"][0] == gsync.APP_HEADER
    row = fake.row_for("신청", app["code"])
    assert row is not None
    cols = dict(zip(gsync.APP_HEADER, row))
    assert cols["상태"] == "접수완료"
    assert cols["산모이름"] == "김산모"
    assert cols["산모연락처"] == "010-1234-5678"
    assert cols["관계"] == "가족" and cols["관계상세"] == "친정어머니"
    assert cols["이용예정기간"] == "15일"
    assert cols["미제출서류"].startswith("산모신생아건강관리사 교육수료증")
    assert cols["제출서류"] == ""
    assert cols["인지경로"] == "맘카페·온라인 커뮤니티"


def test_row_is_updated_not_duplicated(setup):
    """같은 접수번호는 행이 늘지 않고 제자리에서 갱신된다."""
    client, fake, gsync = setup
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    _wait(gsync)
    assert len(fake.rows("신청")) == 1

    client.post(f"/api/admin/applications/{code}/status", headers=ADMIN,
                json={"status": "reviewing", "message": "확인중"})
    client.patch(f"/api/applications/{code}?token={token}",
                 json={**FORM, "voucher_code": "SEOUL-0001"})
    _wait(gsync)

    assert len(fake.rows("신청")) == 1
    cols = dict(zip(gsync.APP_HEADER, fake.row_for("신청", code)))
    assert cols["상태"] == "서류 검토중"
    assert cols["바우처구분코드"] == "SEOUL-0001"


def test_second_application_appends(setup):
    client, fake, gsync = setup
    a = client.post("/api/applications", json={**FORM, "submit": True}).json()
    b = client.post("/api/applications", json={**FORM, "mother_name": "박산모", "submit": True}).json()
    _wait(gsync)
    codes = [r[0] for r in fake.rows("신청")]
    assert sorted(codes) == sorted([a["code"], b["code"]])


def test_document_upload_goes_to_drive_and_links_into_row(setup):
    client, fake, gsync = setup
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    client.post(f"/api/applications/{code}/documents?token={token}",
                data={"doc_type": "health_checkup"},
                files={"file": ("검진결과.pdf", PDF, "application/pdf")})
    _wait(gsync)

    assert len(fake.uploads) == 1
    body = fake.uploads[0]["body"]
    assert PDF in body
    meta = json.loads(body.decode("utf-8", "ignore").split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
    assert meta["name"] == f"{code}__건강검진결과서__검진결과.pdf"
    assert meta["parents"] == ["folder-xyz"]

    cols = dict(zip(gsync.APP_HEADER, fake.row_for("신청", code)))
    assert cols["제출서류"] == "건강검진결과서"
    assert "건강검진결과서" not in cols["미제출서류"]
    assert cols["서류링크"].startswith("https://drive.google.com/file/d/")

    # 링크는 DB에도 남아 재업로드되지 않는다
    detail = client.get(f"/api/applications/{code}?token={token}").json()
    assert detail["documents"][0]["drive_url"].startswith("https://drive.google.com/")
    client.post("/api/admin/sync", headers=ADMIN)
    _wait(gsync)
    assert len(fake.uploads) == 1


def test_rejection_and_missing_docs_land_in_sheet(setup):
    client, fake, gsync = setup
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code = app["code"]
    client.post(f"/api/admin/applications/{code}/status", headers=ADMIN,
                json={"status": "rejected", "message": "결핵 항목이 필요합니다.",
                      "missing_docs": ["health_checkup", "pertussis"]})
    _wait(gsync)

    cols = dict(zip(gsync.APP_HEADER, fake.row_for("신청", code)))
    assert cols["상태"] == "보완요청(반려)"
    assert cols["관리자메시지"] == "결핵 항목이 필요합니다."
    assert cols["부족서류"] == "건강검진결과서, 백일해 예방접종 증명서류"


def test_rejected_document_counts_as_missing_in_sheet(setup):
    client, fake, gsync = setup
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    code, token = app["code"], app["token"]
    up = client.post(f"/api/applications/{code}/documents?token={token}",
                     data={"doc_type": "pertussis"},
                     files={"file": ("접종.pdf", PDF, "application/pdf")}).json()
    client.post(f"/api/admin/documents/{up['documents'][0]['id']}/review", headers=ADMIN,
                json={"status": "rejected", "reason": "판독 불가"})
    _wait(gsync)

    cols = dict(zip(gsync.APP_HEADER, fake.row_for("신청", code)))
    assert cols["제출서류"] == ""
    assert cols["인지경로"] == "맘카페·온라인 커뮤니티"
    assert "백일해 예방접종 증명서류" in cols["미제출서류"]


def test_consultation_row(setup):
    client, fake, gsync = setup
    client.post("/api/consultations", json={"name": "홍길동", "phone": "01011112222",
                                            "preferred_time": "평일 오후", "memo": "문의"})
    _wait(gsync)
    assert fake.tabs["전화상담"][0] == gsync.CONSULT_HEADER
    row = fake.rows("전화상담")[0]
    cols = dict(zip(gsync.CONSULT_HEADER, row))
    assert cols["성함"] == "홍길동" and cols["연락처"] == "010-1111-2222"
    assert cols["상태"] == "대기"

    cid = client.get("/api/admin/consultations", headers=ADMIN).json()["items"][0]["id"]
    client.post(f"/api/admin/consultations/{cid}", headers=ADMIN,
                json={"status": "done", "admin_note": "통화완료"})
    _wait(gsync)
    assert len(fake.rows("전화상담")) == 1        # 갱신이지 추가가 아니다
    assert dict(zip(gsync.CONSULT_HEADER, fake.rows("전화상담")[0]))["상태"] == "완료"


def test_google_failure_does_not_break_requests(setup):
    """구글이 죽어도 접수·업로드는 성공하고, 실패는 상태에 기록된다."""
    client, fake, gsync = setup
    fake.fail_times = 99
    res = client.post("/api/applications", json={**FORM, "submit": True})
    assert res.status_code == 200
    _wait(gsync)

    st = client.get("/api/admin/sync", headers=ADMIN).json()
    assert st["failed"] >= 1
    assert "구글 API 오류" in st["last_error"]
    assert st["enabled"] is True


def test_transient_failure_is_retried(setup):
    client, fake, gsync = setup
    fake.fail_times = 1          # 첫 시도만 실패
    app = client.post("/api/applications", json={**FORM, "submit": True}).json()
    _wait(gsync)
    assert fake.row_for("신청", app["code"]) is not None
    assert client.get("/api/admin/sync", headers=ADMIN).json()["failed"] == 0


def test_sync_all_rebuilds_sheet(setup):
    """연동을 나중에 켠 경우 — 전체 동기화로 기존 신청이 모두 올라간다."""
    client, fake, gsync = setup
    codes = [client.post("/api/applications", json={**FORM, "submit": True}).json()["code"]
             for _ in range(3)]
    client.post("/api/consultations", json={"name": "홍길동", "phone": "01011112222"})
    _wait(gsync)

    fake.tabs.clear()            # 시트를 새로 만든 상황
    res = client.post("/api/admin/sync", headers=ADMIN).json()
    assert res["queued"]["applications"] == 3
    assert res["queued"]["consultations"] == 1
    _wait(gsync)

    assert sorted(r[0] for r in fake.rows("신청")) == sorted(codes)
    assert len(fake.rows("전화상담")) == 1


def test_sync_status_exposes_sheet_link(setup):
    client, _, _ = setup
    st = client.get("/api/admin/sync", headers=ADMIN).json()
    assert st["enabled"] is True
    assert st["drive_enabled"] is True
    assert st["sheet_url"] == "https://docs.google.com/spreadsheets/d/sheet-abc"
    assert client.get("/api/admin/sync").status_code == 401


def test_service_account_json_from_file_path(tmp_path, monkeypatch):
    """JSON 문자열 대신 키 파일 경로를 넣어도 인식한다."""
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(key))
    monkeypatch.setenv("GSHEET_ID", "sheet-abc")
    import server.gsync as gsync_mod
    importlib.reload(gsync_mod)
    assert gsync_mod.enabled() is True

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{망가진 JSON")
    assert gsync_mod.enabled() is False


def test_credentials_are_built_from_the_key(tmp_path, monkeypatch):
    """서비스 계정 키 파싱과 스코프 지정이 실제 google-auth 로 통하는지 확인한다.

    (토큰 갱신은 네트워크가 필요하므로 자격증명 생성까지만 검증한다.)
    """
    google_auth = pytest.importorskip("google.oauth2.service_account")
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = crypto.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GSHEET_ID", "sheet-abc")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "kid",
        "private_key": pem,
        "client_email": "care@p.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    import server.gsync as gsync_mod
    importlib.reload(gsync_mod)

    creds = google_auth.Credentials.from_service_account_info(
        gsync_mod._service_account_info(), scopes=gsync_mod.SCOPES)
    assert creds.service_account_email == "care@p.iam.gserviceaccount.com"
    assert set(creds.scopes) == set(gsync_mod.SCOPES)
