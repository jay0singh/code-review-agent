import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_rejects_missing_signature_when_secret_set(monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret")

    response = client.post(
        "/webhook",
        content=b'{"before": "' + b"0" * 40 + b'"}',
        headers={"X-GitHub-Event": "push"},
    )

    assert response.status_code == 401


def test_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret")
    body = json.dumps({"before": "0" * 40, "commits": []}).encode()

    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sign(body, "s3cret"),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "skipped", "reason": "initial push"}


def test_push_with_no_commits_returns_ok(monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", None)
    body = json.dumps({"before": "e" * 40, "commits": []}).encode()

    response = client.post(
        "/webhook", content=body, headers={"X-GitHub-Event": "push"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commits": []}


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_answers_head_requests():
    response = client.head("/health")

    assert response.status_code == 200


def test_unhandled_event_is_skipped(monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", None)

    response = client.post(
        "/webhook", content=b"{}", headers={"X-GitHub-Event": "star"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "skipped", "reason": "event 'star' not handled"}
