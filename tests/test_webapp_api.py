"""Integration tests for session ownership and the SSE chat entry point."""
import os
import tempfile
from pathlib import Path

import pytest

# Configure the app before importing it: app.py reads these at import time.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="jinrong-api-test-"))
_DOCS = _TEST_ROOT / "docs"
_DOCS.mkdir()
(_DOCS / "faq.md").write_text(
    "# 对账\n\n差错分为长款和短款，需要人工复核。\n",
    encoding="utf-8",
)
os.environ.update({
    "DOCS_DIR": str(_DOCS),
    "DB_PATH": str(_TEST_ROOT / "app.db"),
    "JWT_SECRET": "test-only-secret-not-for-production",
    "ADMIN_INIT_PASSWORD": "admin-test-pass",
    "EMPLOYEE_INIT_PASSWORD": "employee-test-pass",
    "LLM_PROVIDER": "mock",
})

from fastapi.testclient import TestClient  # noqa: E402

import app as webapp  # noqa: E402
import db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(webapp.app) as test_client:
        yield test_client


def login(client, employee_no, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"employee_no": employee_no, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_health_is_ready(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ready"


def test_employee_cannot_spoof_session_owner(client):
    client.post("/api/v1/auth/logout")
    employee = login(client, "employee01", "employee-test-pass")

    response = client.post("/api/v1/sessions", json={"employee_id": "someone-else"})
    assert response.status_code == 200
    session_id = response.json()["data"]["session_id"]

    stored = db.get_session(session_id)
    assert stored is not None
    assert stored["employee_id"] == employee["user_id"]


def test_employee_cannot_chat_into_another_users_session(client):
    client.post("/api/v1/auth/logout")
    login(client, "admin", "admin-test-pass")
    admin_session = client.post("/api/v1/sessions", json={}).json()["data"]["session_id"]

    client.post("/api/v1/auth/logout")
    login(client, "employee01", "employee-test-pass")

    response = client.post(
        "/api/v1/chat",
        json={"session_id": admin_session, "question": "我想查别人的会话"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == 10404


def test_chat_streams_events_and_finishes(client):
    client.post("/api/v1/auth/logout")
    login(client, "employee01", "employee-test-pass")
    session_id = client.post("/api/v1/sessions", json={}).json()["data"]["session_id"]

    response = client.post(
        "/api/v1/chat",
        json={"session_id": session_id, "question": "差错分哪几类？"},
    )
    assert response.status_code == 200
    body = response.text
    assert "event: meta" in body
    assert "event: done" in body
