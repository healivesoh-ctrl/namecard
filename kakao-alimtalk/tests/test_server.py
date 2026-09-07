import pytest
from fastapi.testclient import TestClient

from server.webhooks import normalize_payload


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WORKER_ENABLED", "0")
    monkeypatch.setenv("RULES_PATH", str(tmp_path / "rules.yaml"))
    (tmp_path / "rules.yaml").write_text(
        "provider: console\nsender: '0212345678'\nforms:\n  - id: default\n    messages:\n"
        "      - {name: 접수확인, template: TP1, text: '{{name}}님 접수'}\n"
        "      - {name: 후속, template: TP2, when: {delay: 1d}, text: '후속'}\n", encoding="utf-8")
    from server.app import app
    with TestClient(app) as c:
        yield c


def test_webhook_auth_and_intake(client):
    r = client.post("/webhook/form", json={"answers": {"이름": "홍", "연락처": "010-1234-5678"}})
    assert r.status_code == 401
    r = client.post("/webhook/form?secret=sec", json={"answers": {"이름": "홍", "연락처": "010-1234-5678"}})
    assert r.status_code == 200 and r.json()["ok"] and len(r.json()["jobs"]) == 2
    assert r.json()["phone"] == "010-****-5678"
    # 헤더 인증 + 평면 JSON
    r = client.post("/webhook/form", headers={"X-Webhook-Secret": "sec"}, json={"이름": "김", "휴대폰": "01099998888"})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.post("/webhook/form?secret=sec", json={"answers": {"이름": "번호없음"}})
    assert r.status_code == 200 and not r.json()["ok"]


def test_admin_endpoints(client):
    assert client.get("/api/jobs").status_code == 401
    h = {"X-Admin-Password": "pw"}
    client.post("/webhook/form?secret=sec", json={"answers": {"이름": "홍", "연락처": "010-1234-5678"}})
    jobs = client.get("/api/jobs", headers=h).json()["jobs"]
    assert len(jobs) == 2
    pending = [j for j in jobs if j["status"] == "pending" and j["message"] == "후속"][0]
    r = client.post(f"/api/jobs/{pending['id']}/cancel", headers=h)
    assert r.json()["job"]["status"] == "cancelled"
    r = client.post("/api/send-test", headers=h, json={"phone": "010-1111-2222", "message": "접수확인", "variables": {"이름": "테스트"}})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["job"]["text"] == "테스트님 접수"
    r = client.post("/api/bulk", headers=h, json={"rows": [{"이름": "a", "연락처": "010-1000-2000"}, {"이름": "b", "연락처": "x"}]})
    assert r.json()["accepted"] == 1
    r = client.post("/api/optouts", headers=h, json={"phone": "010-1000-2000"})
    assert r.json()["ok"]
    assert client.get("/api/optouts", headers=h).json()["optouts"][0]["phone"] == "010-1000-2000"
    ov = client.get("/api/overview", headers=h).json()
    assert ov["counts"]["submissions"] >= 2 and ov["forms"][0]["id"] == "default"
    assert client.get("/healthz").json()["ok"]


def test_normalize_tally():
    body = {"eventType": "FORM_RESPONSE", "data": {"formId": "f1", "fields": [
        {"label": "이름", "type": "INPUT_TEXT", "value": "홍길동"},
        {"label": "연락처", "type": "INPUT_PHONE_NUMBER", "value": "+821012345678"},
        {"label": "과정", "type": "MULTIPLE_CHOICE", "value": ["a1"], "options": [{"id": "a1", "text": "AI"}]},
    ]}}
    answers, meta = normalize_payload(body)
    assert answers == {"이름": "홍길동", "연락처": "+821012345678", "과정": "AI"} and meta["form"] == "f1"


def test_normalize_typeform():
    body = {"form_response": {"form_id": "tf", "definition": {"fields": [{"id": "1", "title": "이름"}, {"id": "2", "title": "전화"}, {"id": "3", "title": "과정"}]},
            "answers": [{"type": "text", "text": "홍", "field": {"id": "1"}},
                        {"type": "phone_number", "phone_number": "+82 10 1234 5678", "field": {"id": "2"}},
                        {"type": "choice", "choice": {"label": "AI"}, "field": {"id": "3"}}]}}
    answers, meta = normalize_payload(body)
    assert answers == {"이름": "홍", "전화": "+82 10 1234 5678", "과정": "AI"} and meta["phone"] == "+82 10 1234 5678"


def test_normalize_invalid():
    with pytest.raises(ValueError):
        normalize_payload([1, 2])
