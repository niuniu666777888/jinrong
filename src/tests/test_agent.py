# -*- coding: utf-8 -*-
"""Agent 循环的测试：三个确认门、确认/拒绝、完整链路、越权复核。"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

A = "唐映秋"      # 建单人
B = "傅景"        # 复核人


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean(client):
    """每个用例前把业务数据恢复到初始状态，避免用例之间互相污染。"""
    with db._conn() as c:
        c.execute("DELETE FROM ticket_event")
        c.execute("DELETE FROM ticket")
        c.execute("DELETE FROM verify_record")
        c.execute("DELETE FROM agent_run")
        c.execute("UPDATE diff_record SET status = 'pending'")
    yield


def _data(resp):
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


def _run(client, goal="处理微信渠道的长款差异", operator=A):
    return _data(client.post("/api/agent/run", json={"goal": goal, "operator": operator}))


def _confirm(client, run_id, operator=A, approved=True):
    return _data(client.post(f"/api/agent/{run_id}/confirm",
                             json={"operator": operator, "approved": approved}))


# ---------------------------------------------------------------- 计划与门禁
def test_diff_plan_has_three_confirmation_gates():
    from app.agent_loop import TOOLS, plan_for
    plan = plan_for("处理微信渠道的长款差异")
    gated = [s["tool"] for s in plan if TOOLS[s["tool"]].needs_confirm]
    assert gated == ["create_ticket", "submit_ticket", "review_ticket"]
    assert len(gated) >= 3


def test_readonly_goal_needs_no_confirmation(client):
    d = _run(client, goal="长款和短款有什么区别")
    assert d["status"] == "done"
    assert d["pending"] is None
    assert d["events"][0]["tool"] == "search_policy"


def test_unknown_or_empty_goal(client):
    resp = client.post("/api/agent/run", json={"goal": "  ", "operator": A})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 第一个门：建单
def test_run_stops_before_create_ticket(client):
    d = _run(client)
    assert d["status"] == "waiting_confirm"
    assert d["pending"]["tool"] == "create_ticket"
    assert d["pending"]["preview"]["类型"] == "长款"
    assert d["pending"]["preview"]["金额"] == 1280.0

    # 只读步骤已执行，写操作一次都没发生
    tickets = _data(client.get("/api/tickets"))
    assert tickets == []


def test_confirm_creates_ticket_and_moves_to_next_gate(client):
    d = _run(client)
    d = _confirm(client, d["run_id"])

    assert d["status"] == "waiting_confirm"
    assert d["pending"]["tool"] == "submit_ticket"
    tickets = _data(client.get("/api/tickets"))
    assert len(tickets) == 1
    assert tickets[0]["status"] == "created"
    assert tickets[0]["amount"] == 1280.0


def test_reject_aborts_without_writing(client):
    d = _run(client)
    d = _confirm(client, d["run_id"], approved=False)

    assert d["status"] == "aborted"
    assert d["events"][-1]["result"]["code"] == "REJECTED"
    assert _data(client.get("/api/tickets")) == []


# ---------------------------------------------------------------- 完整链路
def test_full_chain_with_three_confirmations(client):
    d = _run(client)
    d = _confirm(client, d["run_id"])                 # 建单
    d = _confirm(client, d["run_id"])                 # 提交复核
    assert d["pending"]["tool"] == "review_ticket"
    d = _confirm(client, d["run_id"], operator=B)     # 换人复核

    assert d["status"] == "done"
    assert d["pending"] is None
    ticket = _data(client.get("/api/tickets"))[0]
    assert ticket["status"] == "reviewed"
    assert ticket["created_by"] == A
    assert ticket["reviewed_by"] == B


def test_review_by_creator_is_rejected(client):
    d = _run(client)
    d = _confirm(client, d["run_id"])
    d = _confirm(client, d["run_id"])
    resp = client.post(f"/api/agent/{d['run_id']}/confirm",
                       json={"operator": A, "approved": True})
    assert resp.status_code == 403
    # 会话仍停在复核门，工单仍是 submitted
    assert _data(client.get("/api/tickets"))[0]["status"] == "submitted"


# ---------------------------------------------------------------- 状态机
def test_confirm_without_pending_returns_409(client):
    d = _run(client)
    _confirm(client, d["run_id"], approved=False)
    resp = client.post(f"/api/agent/{d['run_id']}/confirm",
                       json={"operator": A, "approved": True})
    assert resp.status_code == 409


def test_state_can_be_read_back(client):
    d = _run(client)
    again = _data(client.get(f"/api/agent/{d['run_id']}"))
    assert again["run_id"] == d["run_id"]
    assert again["status"] == "waiting_confirm"
    assert again["goal"] == "处理微信渠道的长款差异"


def test_unknown_run_returns_404(client):
    resp = client.get("/api/agent/ar_not_exist")
    assert resp.status_code == 404
