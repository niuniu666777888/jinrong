"""接口级测试：问答链路 + 差错处理闭环 + 三条防线。

每个用例用不同的差异（d_001–d_005），互不干扰，因此可以任意顺序执行。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

A = "唐映秋"      # 建单人
B = "傅景"        # 复核人


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _data(resp):
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


# ---------------------------------------------------------------- 基础
def test_health(client):
    d = _data(client.get("/api/health"))
    assert d["status"] == "ready"
    assert d["policies"] >= 9
    assert d["model"] == "mock"


def test_policies_expose_clause_numbers(client):
    rows = _data(client.get("/api/policies"))
    assert any(r["clause_no"] == "2.4" for r in rows)


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
    """超过 500 字走业务码 10401，与空问题保持同一套提示。"""
    resp = client.post("/api/chat", json={"question": "长" * 501})
    assert resp.status_code == 400
    assert resp.json()["code"] == 10401
    assert "500" in resp.json()["message"]


# ---------------------------------------------------------------- 差错：核验
def test_verify_passes_and_issues_token(client):
    d = _data(client.post("/api/diffs/d_001/verify", json={"operator": A}))
    assert d["passed"] is True
    assert d["token"].startswith("vt_")
    assert d["diff"]["diff_id"] == "d_001"


def test_verify_blocks_out_of_window(client):
    d = _data(client.post("/api/diffs/d_004/verify", json={"operator": A}))
    assert d["passed"] is False
    assert d["token"] is None
    assert any(i["rule"] == "OUT_OF_WINDOW" for i in d["issues"])


def test_verify_warns_on_high_amount(client):
    d = _data(client.post("/api/diffs/d_004/verify", json={"operator": A}))
    assert any(i["rule"] == "HIGH_AMOUNT" and i["level"] == "warning" for i in d["issues"])


# ---------------------------------------------------------------- 差错：建单
def test_create_requires_valid_token(client):
    resp = client.post("/api/tickets", json={
        "diff_id": "d_005", "token": "vt_forged.deadbeef",
        "handling": "调单", "operator": A,
    })
    assert resp.status_code == 400
    assert "核验凭证无效" in resp.json()["message"]


def test_create_ticket_uses_server_side_amount(client):
    d = _data(client.post("/api/diffs/d_001/verify", json={"operator": A}))
    t = _data(client.post("/api/tickets", json={
        "diff_id": "d_001", "token": d["token"], "handling": "补单", "operator": A,
    }))
    assert t["amount"] == 1280.00        # 前端没传金额，服务端重查
    assert t["status"] == "created"
    assert t["created_by"] == A


def test_same_diff_cannot_be_ticketed_twice(client):
    # d_001 在上一用例已建单；再核验一次拿到新 token 也建不出第二张
    resp = client.post("/api/diffs/d_001/verify", json={"operator": A})
    assert resp.status_code == 409


def test_token_single_use(client):
    d = _data(client.post("/api/diffs/d_002/verify", json={"operator": A}))
    client.post("/api/tickets", json={"diff_id": "d_002", "token": d["token"],
                                      "handling": "调单", "operator": A})
    resp = client.post("/api/tickets", json={"diff_id": "d_002", "token": d["token"],
                                             "handling": "调单", "operator": A})
    assert resp.status_code == 409


# ---------------------------------------------------------------- 差错：四眼
def test_reviewer_cannot_be_creator(client):
    d = _data(client.post("/api/diffs/d_003/verify", json={"operator": A}))
    t = _data(client.post("/api/tickets", json={"diff_id": "d_003", "token": d["token"],
                                                "handling": "订正", "operator": A}))
    _data(client.post(f"/api/tickets/{t['ticket_id']}/submit", json={"operator": A}))
    resp = client.post(f"/api/tickets/{t['ticket_id']}/review",
                       json={"operator": A, "approve": True})
    assert resp.status_code == 403
    assert "同一人" in resp.json()["message"]


def test_another_operator_can_review(client):
    tickets = _data(client.get("/api/tickets"))
    pending = [t for t in tickets if t["status"] == "submitted" and t["created_by"] == A]
    assert pending, "需要一张已提交的工单"
    t = pending[0]
    d = _data(client.post(f"/api/tickets/{t['ticket_id']}/review",
                          json={"operator": B, "approve": True}))
    assert d["status"] == "reviewed"
    assert d["reviewed_by"] == B


def test_ticket_list_has_events(client):
    tickets = _data(client.get("/api/tickets"))
    assert tickets
    actions = {e["action"] for e in tickets[0]["events"]}
    assert "create" in actions


# ---------------------------------------------------------------- 批次与差异
def test_batches_and_diffs(client):
    batches = _data(client.get("/api/batches"))
    assert len(batches) == 2
    assert all(b["diff_count"] >= 1 for b in batches)

    diffs = _data(client.get("/api/diffs?batch_id=b_wx_1105"))
    assert {d["diff_id"] for d in diffs} >= {"d_001", "d_002", "d_003", "d_004"}
