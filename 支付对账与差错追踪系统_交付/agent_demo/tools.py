#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/tools.py —— Agent 可调用的工具（支付对账版）
================================================================================
七个工具，两类：

  只读：search_policy / query_batch / list_diffs / verify_diff / get_ticket_status
  写入：create_ticket / submit_ticket

三条设计原则（比"工具多"重要得多）：

  ① 写工具只接受 ID，不接受金额。
     模型传 diff_id，金额由服务端从 diff_record 查。模型编个金额也没用。

  ② 建单必须带 verify_diff 签发的 validation_token。
     token 服务端签名 + 落库，模型伪造不出来，
     所以"跳过核验直接建单"在物理上做不到，不靠提示词自觉。

  ③ 工具失败返回结构化错误码，不抛异常。
     异常会中断整个循环；错误码能让模型读到原因后重新决策。
================================================================================
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import db
from db import DAY_MS, execute, new_id, now_ms, q, q1
from policy import search as policy_search

# 生产环境从环境变量注入，演示用固定值
TOKEN_SECRET = b"recon-demo-secret-change-me"
TOKEN_TTL_SECONDS = 600           # 核验凭证 10 分钟有效
BACKTRACK_DAYS = 7                # 回溯窗口（《差错处理规范》2.3）
RISK_THRESHOLD = 3000.0           # 超过这个金额要标注风险等级（2.5）


def ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def err(code: str, message: str, extra: Optional[Dict] = None) -> Dict[str, Any]:
    out = {"ok": False, "code": code, "message": message}
    if extra:
        out.update(extra)
    return out


# ==============================================================================
# 核验凭证
# ==============================================================================
def _sign(payload: Dict[str, Any]) -> str:
    """base64(JSON) + HMAC16。载荷含差异 ID、操作人、过期时间、随机数。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    mac = hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()[:16]
    return f"vt_{body}.{mac}"


def _verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        if not token.startswith("vt_") or "." not in token:
            return None
        body, mac = token[3:].split(".", 1)
        expect = hmac.new(TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(mac, expect):
            return None
        pad = "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return None


# ==============================================================================
# 工具一：检索对账规范
# ==============================================================================
def search_policy(ctx: Dict[str, Any], query: str, category: str = "any") -> Dict[str, Any]:
    """检索对账口径 / 差错处理规范。回答任何"怎么处理"的问题前必须先查。"""
    chunks = policy_search(query, top_k=3)
    if not chunks:
        return err("E_NO_POLICY", "规范中未找到相关条款")

    return ok({"chunks": [
        {"title": f"《{c['doc_title']}》第 {c['clause_no']} 条",
         "text": c["content"],
         "effective_from": c["effective_from"],
         "score": c["score"]}
        for c in chunks
    ]})


# ==============================================================================
# 工具二：查对账批次
# ==============================================================================
def query_batch(ctx: Dict[str, Any], channel_name: str, settle_date: str) -> Dict[str, Any]:
    """
    查某渠道某结算日的对账结果。

    返回批次概览 + 差异分类计数，模型据此决定下一步查哪一类差异。
    """
    ch = q1("SELECT * FROM channel WHERE name = ?", (channel_name,))
    if not ch:
        return err("E_NO_CHANNEL", f"未知渠道：{channel_name}")

    batch = q1("SELECT * FROM recon_batch WHERE channel_id = ? AND settle_date = ?",
               (ch["id"], settle_date))
    if not batch:
        return err("E_NO_BATCH", f"{channel_name} {settle_date} 没有对账批次，可能尚未日切")

    breakdown = {
        r["diff_type"]: r["c"]
        for r in q("SELECT diff_type, COUNT(*) c FROM diff_record WHERE batch_id = ? "
                   "GROUP BY diff_type", (batch["id"],))
    }

    return ok({
        "batch_id": batch["id"],
        "channel": ch["name"],
        "settle_date": batch["settle_date"],
        "txn_count": batch["txn_count"],
        "total_amount": batch["total_amount"],
        "diff_count": batch["diff_count"],
        "diff_breakdown": breakdown,
        "status": batch["status"],
    })


# ==============================================================================
# 工具三：列差异明细
# ==============================================================================
def list_diffs(ctx: Dict[str, Any], batch_id: str,
               diff_type: str = "", limit: int = 5) -> Dict[str, Any]:
    """按批次和类型列差异。默认只给 5 条，避免上下文被刷屏。"""
    sql = "SELECT * FROM diff_record WHERE batch_id = ?"
    params: List[Any] = [batch_id]
    if diff_type:
        sql += " AND diff_type = ?"
        params.append(diff_type)
    sql += " ORDER BY found_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 20)))

    rows = q(sql, tuple(params))
    if not rows:
        return err("E_NO_DIFF", "该条件下没有差异记录")

    return ok({"count": len(rows), "items": [
        {"diff_id": r["id"], "diff_type": r["diff_type"],
         "channel_txn_no": r["channel_txn_no"],
         "internal_order_no": r["internal_order_no"],
         "amount": r["amount"], "status": r["status"]}
        for r in rows
    ]})


# ==============================================================================
# 工具四：核验差异（写 verify_record + 签发 token）
# ==============================================================================
def verify_diff(ctx: Dict[str, Any], diff_id: str) -> Dict[str, Any]:
    """
    核验一条差异是否成立。返回 passed / issues / validation_token。

    检查项（全跑完再汇总，不短路——一次说清所有问题比让模型来回试更好）：
      1. 差异是否存在、状态是否为 pending
      2. 是否在 7 天回溯窗口内（超窗不得自动销账）
      3. 是否已经有工单（防重复建单）
      4. 类型与证据是否自洽：长款要求渠道有流水且内部无单；短款反之
      5. 金额超过 3000 打风险标记（warning，不阻断）
    """
    d = q1("SELECT * FROM diff_record WHERE id = ?", (diff_id,))
    if not d:
        return err("E_NO_DIFF", f"差异记录 {diff_id} 不存在")
    if d["status"] != "pending":
        return err("E_STATE", f"该差异状态为 {d['status']}，不能重复处理")

    issues: List[Dict[str, Any]] = []

    # --- 2. 回溯窗口 ---
    age_days = (now_ms() - d["found_at"]) / DAY_MS
    if age_days > BACKTRACK_DAYS:
        issues.append({
            "rule": "OUT_OF_WINDOW", "level": "error",
            "message": f"差异已产生 {age_days:.1f} 天，超过 {BACKTRACK_DAYS} 天回溯窗口，"
                       f"须报资金风险经理处理",
            "clause": "《差错处理规范》2.3",
        })

    # --- 3. 重复建单 ---
    existed = q1("SELECT ticket_no FROM ticket WHERE diff_id = ?", (diff_id,))
    if existed:
        issues.append({
            "rule": "DUPLICATE", "level": "error",
            "message": f"该差异已有工单 {existed['ticket_no']}，不能重复建单",
            "clause": "《差错处理规范》2.4",
        })

    # --- 4. 类型与证据自洽 ---
    if d["diff_type"] == "长款":
        if not d["channel_txn_no"] or d["internal_order_no"]:
            issues.append({
                "rule": "EVIDENCE_MISMATCH", "level": "error",
                "message": "长款应满足：渠道有流水、内部无订单。当前证据不符",
                "clause": "《差错处理规范》2.1",
            })
    elif d["diff_type"] == "短款":
        if not d["internal_order_no"]:
            issues.append({
                "rule": "EVIDENCE_MISMATCH", "level": "error",
                "message": "短款应满足：内部有成功订单、渠道无流水。当前证据不符",
                "clause": "《差错处理规范》2.2",
            })

    # --- 5. 大额风险标记 ---
    if d["amount"] > RISK_THRESHOLD:
        issues.append({
            "rule": "HIGH_AMOUNT", "level": "warning",
            "message": f"单笔 {d['amount']:.2f} 元超过 {RISK_THRESHOLD:.0f} 元，"
                       f"建单须标注风险等级并抄送风险经理",
            "clause": "《差错处理规范》2.5",
        })

    passed = not any(i["level"] == "error" for i in issues)

    token = None
    expire_at = None
    if passed:
        expire_at = now_ms() + TOKEN_TTL_SECONDS * 1000
        token = _sign({"diff": d["id"], "uid": ctx["user_id"],
                       "exp": expire_at, "nonce": new_id("n")})

    execute("INSERT INTO verify_record (id, diff_id, passed, issues, token, expire_at, "
            "used, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (new_id("v"), d["id"], 1 if passed else 0,
             json.dumps(issues, ensure_ascii=False), token, expire_at, now_ms()))

    return ok({
        "diff_id": d["id"],
        "diff_type": d["diff_type"],
        "amount": d["amount"],
        "passed": passed,
        "issues": issues,
        "validation_token": token,
        "expire_in_seconds": TOKEN_TTL_SECONDS if passed else 0,
    })


# ==============================================================================
# 工具五：建差错工单（唯一写入口，也是防守重点）
# ==============================================================================
HANDLING_BY_TYPE = {
    "长款": ["补单", "退款"],
    "短款": ["调单", "补偿扣款"],
    "金额不符": ["订正"],
    "状态不符": ["订正"],
}


def create_ticket(ctx: Dict[str, Any], diff_id: str, validation_token: str,
                  handling: str, note: str = "") -> Dict[str, Any]:
    """
    创建差错工单。

    四道检查，缺一不可：
      ① 验 HMAC（模型伪造不出来）
      ② token 里的差异 ID 必须与传入一致（防张冠李戴）
      ③ 库里核验记录必须 passed=1、未使用、未过期
      ④ 金额、类型从 diff_record 重查，忽略模型传值
    """
    d = q1("SELECT * FROM diff_record WHERE id = ?", (diff_id,))
    if not d:
        return err("E_NO_DIFF", f"差异记录 {diff_id} 不存在")

    allowed = HANDLING_BY_TYPE.get(d["diff_type"], [])
    if handling not in allowed:
        return err("E_BAD_HANDLING",
                   f"{d['diff_type']} 的处理方式只能是 {'/'.join(allowed)}，收到 {handling}")

    # --- ① 验签 ---
    payload = _verify_token(validation_token)
    if not payload:
        return err("E_TOKEN_INVALID", "核验凭证无效，请重新核验差异后再建单")

    # --- ② 载荷一致性 ---
    if payload.get("diff") != diff_id:
        return err("E_TOKEN_MISMATCH", "核验凭证与当前差异不匹配，请重新核验")

    # --- ③ 库记录 ---
    rec = q1("SELECT * FROM verify_record WHERE token = ?", (validation_token,))
    if not rec:
        return err("E_TOKEN_INVALID", "核验凭证不存在，请重新核验")
    if not rec["passed"]:
        return err("E_NOT_PASSED", "该差异未通过核验，不能建单")
    if rec["used"]:
        return err("E_TOKEN_USED", "该凭证已使用过，不能重复建单")
    if (rec["expire_at"] or 0) < now_ms():
        return err("E_TOKEN_EXPIRED", "核验已过期（超过 10 分钟），请重新核验")

    # --- ④ 落库（金额/类型全部取库里，不信模型）---
    ticket_id = new_id("t")
    ticket_no = "CT" + datetime.now().strftime("%Y%m%d") + "-" + ticket_id[-4:].upper()

    execute("INSERT INTO ticket (id, ticket_no, diff_id, diff_type, handling, amount, "
            "status, owner, created_at) VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)",
            (ticket_id, ticket_no, diff_id, d["diff_type"], handling, d["amount"],
             ctx["user_id"], now_ms()))
    execute("UPDATE diff_record SET status = 'ticketed' WHERE id = ?", (diff_id,))
    execute("UPDATE verify_record SET used = 1 WHERE token = ?", (validation_token,))

    return ok({
        "ticket_id": ticket_id,
        "ticket_no": ticket_no,
        "diff_id": diff_id,
        "diff_type": d["diff_type"],
        "handling": handling,
        "amount": d["amount"],
        "status": "created",
        "risk_level": "high" if d["amount"] > RISK_THRESHOLD else "normal",
        "note": note,
    })


# ==============================================================================
# 工具六：提交复核
# ==============================================================================
def submit_ticket(ctx: Dict[str, Any], ticket_id: str) -> Dict[str, Any]:
    """提交复核。不可逆，仅 created 状态可提交。"""
    t = q1("SELECT * FROM ticket WHERE id = ?", (ticket_id,))
    if not t:
        return err("E_NO_TICKET", "工单不存在")
    if t["status"] != "created":
        return err("E_STATE", f"工单状态为 {t['status']}，不能重复提交")

    execute("UPDATE ticket SET status = 'submitted' WHERE id = ?", (ticket_id,))
    return ok({"ticket_no": t["ticket_no"], "status": "submitted",
               "next": "已进入复核队列，须由另一名经办人签核（双人复核）"})


# ==============================================================================
# 工具七：查工单进度
# ==============================================================================
def get_ticket_status(ctx: Dict[str, Any], ticket_no: str) -> Dict[str, Any]:
    t = q1("SELECT * FROM ticket WHERE ticket_no = ?", (ticket_no,))
    if not t:
        return err("E_NO_TICKET", f"未找到工单 {ticket_no}")
    return ok({
        "ticket_no": t["ticket_no"],
        "status": t["status"],
        "diff_type": t["diff_type"],
        "handling": t["handling"],
        "amount": t["amount"],
        "created_at": datetime.fromtimestamp(t["created_at"] / 1000).strftime("%Y-%m-%d %H:%M"),
    })


# ==============================================================================
# 注册表
# ==============================================================================
TOOL_SPECS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "search_policy",
        "description": "检索对账口径与差错处理规范。回答“怎么处理/标准是什么”前必须先调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "category": {"type": "string", "enum": ["caliber", "handling", "process", "any"]},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "query_batch",
        "description": "查询某渠道某结算日的对账批次结果与差异分类统计。",
        "parameters": {"type": "object", "properties": {
            "channel_name": {"type": "string", "description": "如 微信支付"},
            "settle_date": {"type": "string", "description": "YYYY-MM-DD"},
        }, "required": ["channel_name", "settle_date"]}}},
    {"type": "function", "function": {
        "name": "list_diffs",
        "description": "按批次列出差异明细，可按类型过滤。",
        "parameters": {"type": "object", "properties": {
            "batch_id": {"type": "string"},
            "diff_type": {"type": "string", "enum": ["长款", "短款", "金额不符", "状态不符"]},
            "limit": {"type": "integer"},
        }, "required": ["batch_id"]}}},
    {"type": "function", "function": {
        "name": "verify_diff",
        "description": ("核验差异是否成立。passed=true 时返回 validation_token，"
                        "create_ticket 必须携带。建单前必须调用。"),
        "parameters": {"type": "object", "properties": {
            "diff_id": {"type": "string"},
        }, "required": ["diff_id"]}}},
    {"type": "function", "function": {
        "name": "create_ticket",
        "description": ("创建差错工单。必须携带 verify_diff 返回的 validation_token，"
                        "系统会二次校验，不通过会被拒绝。"),
        "parameters": {"type": "object", "properties": {
            "diff_id": {"type": "string"},
            "validation_token": {"type": "string"},
            "handling": {"type": "string", "enum": ["补单", "退款", "调单", "补偿扣款", "订正"]},
            "note": {"type": "string", "maxLength": 200},
        }, "required": ["diff_id", "validation_token", "handling"]}}},
    {"type": "function", "function": {
        "name": "submit_ticket",
        "description": "提交工单复核。仅在用户明确确认后调用。",
        "parameters": {"type": "object", "properties": {
            "ticket_id": {"type": "string"},
        }, "required": ["ticket_id"]}}},
    {"type": "function", "function": {
        "name": "get_ticket_status",
        "description": "按工单号查询处理进度。",
        "parameters": {"type": "object", "properties": {
            "ticket_no": {"type": "string"},
        }, "required": ["ticket_no"]}}},
]

TOOL_IMPL = {
    "search_policy": search_policy,
    "query_batch": query_batch,
    "list_diffs": list_diffs,
    "verify_diff": verify_diff,
    "create_ticket": create_ticket,
    "submit_ticket": submit_ticket,
    "get_ticket_status": get_ticket_status,
}

# 写完就改不了的工具：执行前必须人工确认
GUARDED_TOOLS = {"create_ticket", "submit_ticket"}
