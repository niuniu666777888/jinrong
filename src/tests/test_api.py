# -*- coding: utf-8 -*-
"""接口级测试：登录、问答链路、差错处理闭环、三条防线。

每个用例默认以员工身份登录；需要复核权的用例显式切换为管理员。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

EMP = {"employee_no": "employee01", "password": "emp123"}
ADMIN = {"employee_no": "admin", "password": "admin123"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, cred):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json=cred)
    assert r.status_code == 200, r.text
    return r.json()["data"]


@pytest.fixture(autouse=True)
def as_employee(client):
    login(client, EMP)
    yield


def _data(resp):
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


# ---------------------------------------------------------------- 认证
def test_health_is_public(client):
    client.post("/api/auth/logout")
    d = _data(client.get("/api/health"))
    assert d["status"] == "ready"
    assert d["policies"] >= 9


def test_api_requires_login(client):
    client.post("/api/auth/logout")
    resp = client.get("/api/batches")
    assert resp.status_code == 401
    assert resp.json()["code"] == 10402


def test_login_rejects_wrong_password(client):
    client.post("/api/auth/logout")
    resp = client.post("/api/auth/login", json={"employee_no": "employee01", "password": "nope"})
    assert resp.status_code == 401
    assert "不正确" in resp.json()["message"]


def test_me_returns_current_user(client):
    d = _data(client.get("/api/auth/me"))
    assert d["employee_no"] == "employee01"
    assert d["name"] == "唐映秋"
    assert d["role"] == "employee"


# ---------------------------------------------------------------- 问答
def test_chat_streams_with_sources(client):
    resp = client.post("/api/chat", json={"question": "长款和短款有什么区别？"})
    assert resp.status_code == 200
    text = resp.text
    assert "event: meta" in text
    assert "event: sources" in text
    assert "event: delta" in text
    assert "event: done" in text
    assert "degraded\": true" not in text


def test_chat_falls_back_when_nothing_matches(client):
    resp = client.post("/api/chat", json={"question": "今天晚饭吃什么"})
    text = resp.text
    assert "未找到" in text or "degraded\": true" in text


def test_chat_rejects_empty_question(client):
    resp = client.post("/api/chat", json={"question": "   "})
    assert resp.status_code == 400
    assert resp.json()["code"] == 10401


def test_chat_rejects_too_long_question(client):
    resp = client.post("/api/chat", json={"question": "长" * 501})
    assert resp.status_code == 400
    assert resp.json()["code"] == 10401
    assert "500" in resp.json()["message"]


# ---------------------------------------------------------------- 核验
def test_verify_passes_and_issues_token(client):
    d = _data(client.post("/api/diffs/d_001/verify"))
    assert d["passed"] is True
    assert d["token"].startswith("vt_")
    assert d["diff"]["diff_id"] == "d_001"


def test_verify_blocks_out_of_window(client):
    d = _data(client.post("/api/diffs/d_004/verify"))
    assert d["passed"] is False
    assert d["token"] is None
    assert any(i["rule"] == "OUT_OF_WINDOW" for i in d["issues"])


def test_verify_warns_on_high_amount(client):
    d = _data(client.post("/api/diffs/d_004/verify"))
    assert any(i["rule"] == "HIGH_AMOUNT" and i["level"] == "warning" for i in d["issues"])


# ---------------------------------------------------------------- 建单
def test_create_requires_valid_token(client):
    resp = client.post("/api/tickets", json={
        "diff_id": "d_005", "token": "vt_forged.deadbeef", "handling": "调单",
    })
    assert resp.status_code == 400
    assert "核验凭证无效" in resp.json()["message"]


def test_create_ticket_uses_server_side_amount(client):
    d = _data(client.post("/api/diffs/d_001/verify"))
    t = _data(client.post("/api/tickets", json={
        "diff_id": "d_001", "token": d["token"], "handling": "补单",
    }))
    assert t["amount"] == 1280.00
    assert t["status"] == "created"
    assert t["created_by"] == "唐映秋"


def test_same_diff_cannot_be_ticketed_twice(client):
    resp = client.post("/api/diffs/d_001/verify")
    assert resp.status_code == 409


def test_token_single_use(client):
    d = _data(client.post("/api/diffs/d_002/verify"))
    client.post("/api/tickets", json={"diff_id": "d_002", "token": d["token"], "handling": "调单"})
    resp = client.post("/api/tickets", json={"diff_id": "d_002", "token": d["token"],
                                             "handling": "调单"})
    assert resp.status_code == 409


# ---------------------------------------------------------------- 权限
def test_employee_cannot_review(client):
    """复核权只给风险岗（admin），对账岗即使换人也不能复核。"""
    d = _data(client.post("/api/diffs/d_003/verify"))
    t = _data(client.post("/api/tickets", json={"diff_id": "d_003", "token": d["token"],
                                                "handling": "订正"}))
    client.post(f"/api/tickets/{t['ticket_id']}/submit")
    resp = client.post(f"/api/tickets/{t['ticket_id']}/review", json={"approve": True})
    assert resp.status_code == 403
    assert "权限" in resp.json()["message"]


def test_reviewer_cannot_be_creator(client):
    """管理员自己建的单，自己不能复核（四眼原则）。"""
    login(client, ADMIN)
    d = _data(client.post("/api/diffs/d_005/verify"))
    t = _data(client.post("/api/tickets", json={"diff_id": "d_005", "token": d["token"],
                                                "handling": "调单"}))
    client.post(f"/api/tickets/{t['ticket_id']}/submit")
    resp = client.post(f"/api/tickets/{t['ticket_id']}/review", json={"approve": True})
    assert resp.status_code == 403
    assert "同一人" in resp.json()["message"]


def test_another_operator_can_review(client):
    """员工建单、管理员复核 → 通过。"""
    tickets = _data(client.get("/api/tickets"))
    pending = [t for t in tickets if t["status"] == "submitted" and t["created_by"] == "唐映秋"]
    assert pending, "需要一张员工提交的工单"
    login(client, ADMIN)
    d = _data(client.post(f"/api/tickets/{pending[0]['ticket_id']}/review",
                          json={"approve": True}))
    assert d["status"] == "reviewed"
    assert d["reviewed_by"] == "傅景"


# ---------------------------------------------------------------- 批次与差异
def test_batches_and_diffs(client):
    batches = _data(client.get("/api/batches"))
    assert len(batches) == 2
    assert all(b["diff_count"] >= 1 for b in batches)
    diffs = _data(client.get("/api/diffs?batch_id=b_wx_1105"))
    assert {d["diff_id"] for d in diffs} >= {"d_001", "d_002", "d_003", "d_004"}
