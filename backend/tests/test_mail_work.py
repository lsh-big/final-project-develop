import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx

from app.features import mail_work
from app.main import app
from test_purchases import client


WORK = "/api/v1/ai-mail/demo-work"
THREAD = WORK + "/demo-thread-01"
INBOX = "/api/v1/ai-mail/demo-import"


def test_reply_uses_only_confirmed_facts(client, monkeypatch):
    monkeypatch.setattr(mail_work, "analysis_config", lambda: ("", ""))
    client.get(WORK)
    facts = "금요일 오전 납품은 어렵지만 같은 날 오후 2시에는 가능합니다."
    response = client.post(WORK + "/demo-thread-15/draft", json={"message_id": "demo-15-2", "confirmed_facts": facts})
    assert response.status_code == 200
    body = response.json()
    assert facts in body["text"] and "실제 납기와 가능한 일정" in body["checks"]
    assert body["reply"]["subject"] == "RE: 이번 주 입고 일정 확정"
    assert "@example.invalid" in body["reply"]["recipient"]
    assert client.get(WORK + "/demo-thread-15/sends").json() == []


def test_demo_send_is_scoped_and_idempotent(client):
    body = {"request_id": str(uuid4()), "message_id": "demo-01-2", "text": "검토 결과를 회신드립니다."}
    endpoint = THREAD + "/sends"
    assert client.post(endpoint, json=body).status_code == 403
    actor = client.get(WORK).json()["actor"]
    original = client.get(INBOX).json()
    assert client.post(endpoint, json={**body, "recipient": "other@example.com"}).status_code == 422
    assert client.post(endpoint, json={**body, "message_id": "demo-02-1"}).status_code == 404
    assert client.post(endpoint, json=body, headers={"Origin": "https://other.example"}).status_code == 403
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(endpoint, json=body), range(2)))
    assert all(response.status_code == 200 for response in responses)
    sent = responses[0].json()
    assert responses[1].json() == sent
    assert sent["status"] == "simulated" and sent["actor"] == actor and sent["at"]
    assert sent["recipient"].endswith("<vendor-01@example.invalid>")
    assert client.post(endpoint, json={**body, "text": "다른 답장"}).status_code == 409
    assert client.get(endpoint).json() == [sent]
    with TestClient(app) as other:
        other.get(WORK)
        assert other.get(endpoint).json() == []
    assert client.get(WORK).json()["threads"] == {}
    assert client.get(INBOX).json() == original


def test_draft_and_work_persist_without_changing_mail_read_state(client, monkeypatch):
    monkeypatch.setattr(mail_work, "analysis_config", lambda: ("", ""))
    original = client.get(INBOX).json()
    login = client.get(WORK)
    assert login.status_code == 200 and login.json()["threads"] == {}
    assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
    actor = login.json()["actor"]
    assert client.post(THREAD + "/draft", json={"message_id": "demo-02-1"}).status_code == 404
    draft = client.post(THREAD + "/draft", json={"message_id": "demo-01-2"}).json()
    assert draft["source"] == "demo" and draft["text"]
    assert "회신할 가격과 수량" in draft["checks"]
    assert "내부 검토 결과와 승인 여부" in draft["checks"]
    assert "원으로" not in draft["text"] and "첨부했습니다" not in draft["text"]
    assert client.get(WORK).json()["threads"] == {}

    body = {"version": 0, "status": "in_progress", "assignee": "홍길동", "next_action": "견적 검토", "memo": "구매팀 확인 요청"}
    response = client.put(THREAD, json=body)
    assert response.status_code == 200
    started = response.json()
    assert started["version"] == 1 and started["assignee"] == "홍길동"
    assert {event["field"] for event in started["history"]} == {"status", "assignee", "next_action", "memo"}
    assert all(event["actor"] == actor and event["at"].endswith("+09:00") for event in started["history"])
    assert client.put(THREAD, json=body).status_code == 409
    done = client.put(THREAD, json={**body, "version": 1, "status": "done", "memo": ""}).json()
    assert done["status"] == "done" and done["history"][-1]["before"] == "in_progress"
    assert client.post(INBOX).json() == original
    assert client.get(WORK).json()["threads"]["demo-thread-01"] == done
    reopened = client.put(THREAD, json={**body, "version": 2, "status": "in_progress", "memo": ""}).json()
    assert reopened["status"] == "in_progress"
    assert reopened["history"][-1]["before"] == "done"
    assert client.get(INBOX).json()["counts"]["unread"] == 30


def test_demo_work_rejects_unscoped_updates_and_other_sessions(client):
    body = {"version": 0, "status": "done", "assignee": "", "next_action": ""}
    assert client.put(THREAD, json=body).status_code == 403
    assert client.post(THREAD + "/draft", json={"message_id": "demo-01-1"}).status_code == 403
    owner = client.get(WORK).json()["actor"]
    assert client.put(THREAD, json={**body, "actor": "위조 사용자"}).status_code == 422
    assert client.put(THREAD, json=body, headers={"Origin": "https://other.example"}).status_code == 403
    assert client.put(WORK + "/unknown", json=body).status_code == 404
    saved = client.put(THREAD, json=body).json()
    with TestClient(app) as other:
        isolated = other.get(WORK).json()
        assert isolated["threads"] == {} and isolated["actor"] != owner
        assert other.put(THREAD, json={**body, "version": saved["version"], "status": "pending"}).status_code == 409
        assert other.put(THREAD, json={**body, "assignee": "다른 사용자"}).status_code == 200
    assert client.get(WORK).json()["threads"]["demo-thread-01"] == saved


def test_local_dev_proxy_accepts_only_same_origin_browser_requests(client):
    draft = {"message_id": "demo-01-1"}
    with TestClient(app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000)) as local:
        local.get(WORK)
        headers = {"Origin": "http://127.0.0.1:5173", "Sec-Fetch-Site": "same-origin"}
        assert local.post(THREAD + "/draft", json=draft, headers=headers).status_code == 200
        assert local.post(THREAD + "/draft", json=draft, headers={**headers, "Sec-Fetch-Site": "same-site"}).status_code == 403
        assert local.post(THREAD + "/draft", json=draft, headers={**headers, "Origin": "https://other.example"}).status_code == 403


def test_draft_ai_can_select_only_an_allowed_reply_template(client, monkeypatch):
    monkeypatch.setattr(mail_work, "analysis_config", lambda: ("test-key", "test-model"))
    outputs = [{"style": "clarify"}, {"style": "follow_up", "body": "납기를 확정했고 첨부를 완료했습니다."}]
    requests = []

    def provider(url, **options):
        payload = options["json"]
        requests.append(payload)
        source = json.loads(payload["input"])
        assert source["target"] == "demo-01-1"
        assert [mail["id"] for mail in source["conversation"]] == ["demo-01-1", "demo-01-2"]
        assert "tools" not in payload and payload["store"] is False
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(outputs.pop(0))}]},
            ],
        })

    monkeypatch.setattr(mail_work.httpx, "post", provider)
    client.get(WORK)
    assert client.post(THREAD + "/draft", json={"message_id": "demo-01-1"}).json()["source"] == "assisted"
    fallback = client.post(THREAD + "/draft", json={"message_id": "demo-01-1"}).json()
    assert fallback["source"] == "demo" and "납기를 확정" not in fallback["text"]
    assert len(requests) == 2 and client.get(WORK).json()["threads"] == {}
