#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/db.py —— 支付对账 Agent 的 SQLite 数据层
================================================================================
表结构围绕"渠道账单 → 对账批次 → 差异记录 → 差错工单"这条主线：

    channel        支付渠道（微信/支付宝/银联）
    recon_batch    对账批次：某渠道某日的对账结果
    diff_record    差异记录：长款/短款/金额不符/状态不符
    verify_record  差异核验记录（含 validation_token，建单必须带）
    ticket         差错工单
    policy_chunk   对账口径与差错处理规范（供检索）
    agent_log      每轮工具调用日志（阶段 5 分析用）

关键设计：verify_record 单独存 token。
create_ticket 只认"库里存在、未过期、未被使用"的 token，
模型传什么字符串都没用——这是"不能跳过核验直接建单"的物理保证。
================================================================================
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).with_name("agent_demo.db")
DAY_MS = 86400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id          TEXT PRIMARY KEY,
  employee_no TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  dept        TEXT NOT NULL,
  role        TEXT NOT NULL               -- recon_accountant / risk_manager
);

CREATE TABLE IF NOT EXISTS channel (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  settle_lag_days INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recon_batch (
  id           TEXT PRIMARY KEY,
  channel_id   TEXT NOT NULL,
  settle_date  TEXT NOT NULL,             -- YYYY-MM-DD
  txn_count    INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  diff_count   INTEGER NOT NULL,
  status       TEXT NOT NULL              -- reconciled / pending
);

CREATE TABLE IF NOT EXISTS diff_record (
  id                TEXT PRIMARY KEY,
  batch_id          TEXT NOT NULL,
  diff_type         TEXT NOT NULL,        -- 长款 / 短款 / 金额不符 / 状态不符
  channel_txn_no    TEXT,                 -- 渠道流水号
  internal_order_no TEXT,                 -- 内部订单号（长款时通常为空）
  amount            REAL NOT NULL,
  currency          TEXT NOT NULL DEFAULT 'CNY',
  found_at          INTEGER NOT NULL,     -- 发现时间（毫秒）
  status            TEXT NOT NULL,        -- pending / ticketed / closed
  channel_ref       TEXT,                 -- 渠道侧证据摘要
  internal_ref      TEXT                  -- 内部侧证据摘要
);

CREATE TABLE IF NOT EXISTS verify_record (
  id         TEXT PRIMARY KEY,
  diff_id    TEXT NOT NULL,
  passed     INTEGER NOT NULL,
  issues     TEXT NOT NULL,               -- JSON 数组
  token      TEXT,                        -- 通过时签发
  expire_at  INTEGER,
  used       INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket (
  id         TEXT PRIMARY KEY,
  ticket_no  TEXT NOT NULL UNIQUE,
  diff_id    TEXT NOT NULL,
  diff_type  TEXT NOT NULL,
  handling   TEXT NOT NULL,               -- 补单 / 退款 / 调单 / 订正
  amount     REAL NOT NULL,
  status     TEXT NOT NULL,               -- created / submitted / reviewed
  owner      TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_chunk (
  id             TEXT PRIMARY KEY,
  doc_title      TEXT NOT NULL,
  clause_no      TEXT NOT NULL,
  category       TEXT NOT NULL,           -- caliber / handling / process
  content        TEXT NOT NULL,
  effective_from TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_log (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  round_no   INTEGER NOT NULL,
  tool_name  TEXT NOT NULL,
  args_json  TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  blocked    INTEGER NOT NULL DEFAULT 0,
  summary    TEXT,
  elapsed_ms INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diff_batch ON diff_record(batch_id, diff_type);
CREATE INDEX IF NOT EXISTS idx_log_session ON agent_log(session_id, round_no);
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def q(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def q1(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> None:
    conn = connect()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def log_call(session_id: str, round_no: int, tool_name: str, args: Dict[str, Any],
             ok: bool, summary: str, elapsed_ms: int, blocked: bool = False) -> None:
    import json
    execute(
        "INSERT INTO agent_log (id, session_id, round_no, tool_name, args_json, ok, "
        "blocked, summary, elapsed_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("log"), session_id, round_no, tool_name,
         json.dumps(args, ensure_ascii=False), 1 if ok else 0,
         1 if blocked else 0, summary[:300], elapsed_ms, now_ms()),
    )


# ==============================================================================
# 种子数据
# ==============================================================================
def seed() -> None:
    if q1("SELECT 1 AS x FROM user LIMIT 1"):
        return

    execute("INSERT INTO user (id, employee_no, name, dept, role) VALUES (?, ?, ?, ?, ?)",
            ("u_tang", "employee01", "唐映秋", "支付运营部", "recon_accountant"))

    execute("INSERT INTO channel (id, name, settle_lag_days) VALUES (?, ?, ?)",
            ("ch_wx", "微信支付", 1))
    execute("INSERT INTO channel (id, name, settle_lag_days) VALUES (?, ?, ?)",
            ("ch_alipay", "支付宝", 1))

    # 昨天（相对当前时间）的微信对账批次
    execute("INSERT INTO recon_batch (id, channel_id, settle_date, txn_count, "
            "total_amount, diff_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("b_wx_1029", "ch_wx", "2026-10-29", 812345, 8432100.55, 3, "reconciled"))

    now = now_ms()
    diffs = [
        # id, batch, type, channel_txn, internal_order, amount, found_at, status, refs
        ("d_001", "b_wx_1029", "长款", "WX202610290001", None, 1280.00, now - 2 * DAY_MS,
         "pending", "渠道账单第 8821 行：交易成功，金额 1280.00", "内部订单表无此订单"),
        ("d_002", "b_wx_1029", "短款", "WX202610290042", "ORD202610290042", 320.00,
         now - 2 * DAY_MS, "pending",
         "渠道账单无此流水", "内部流水状态=支付成功"),
        ("d_003", "b_wx_1029", "金额不符", "WX202610290088", "ORD202610290088", 15.00,
         now - 1 * DAY_MS, "pending",
         "渠道金额 1015.00", "内部金额 1000.00，差额 15.00 疑为手续费"),
        ("d_004", "b_wx_1029", "长款", "WX202610180007", None, 5000.00, now - 9 * DAY_MS,
         "pending", "渠道账单第 112 行", "内部无订单"),
        ("d_005", "b_wx_1029", "长款", "WX202610290133", None, 88.00, now - 1 * DAY_MS,
         "pending", "渠道账单第 9033 行", "内部无订单"),
    ]
    for row in diffs:
        execute("INSERT INTO diff_record (id, batch_id, diff_type, channel_txn_no, "
                "internal_order_no, amount, currency, found_at, status, channel_ref, "
                "internal_ref) VALUES (?, ?, ?, ?, ?, ?, 'CNY', ?, ?, ?, ?)",
                (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                 row[8], row[9]))

    policies = [
        ("p1", "对账口径说明", "1.1", "caliber",
         "对账口径：渠道按结算日统计，内部按支付日统计。跨日退款需按支付日归属，"
         "不得直接计入结算日，否则会产生虚假差异。", "2026-01-01"),
        ("p2", "差错处理规范", "2.1", "handling",
         "长款：渠道有交易记录、内部无有效订单（平台多收）。处理方式为补单或原路退款，"
         "须在回溯窗口内处理完毕。", "2026-01-01"),
        ("p3", "差错处理规范", "2.2", "handling",
         "短款：内部显示支付成功、渠道无对应流水（平台少收）。处理方式为向渠道调单核查，"
         "必要时补偿扣款或冻结订单。", "2026-01-01"),
        ("p4", "差错处理规范", "2.3", "process",
         "差异记录自发现之日起 7 天为回溯窗口，窗口内未平账的须建差错工单；"
         "超过 7 天的差异不得自动销账，须报资金风险经理。", "2026-01-01"),
        ("p5", "差错处理规范", "2.4", "process",
         "销账必须双人复核：经办人提交、复核人签核，系统强制校验发起人与复核人不同。",
         "2026-01-01"),
        ("p6", "差错处理规范", "2.5", "process",
         "单笔差异金额超过 3000 元的，建单时须标注风险等级并抄送资金风险经理。",
         "2026-01-01"),
    ]
    for pid, title, clause, cat, content, eff in policies:
        execute("INSERT INTO policy_chunk (id, doc_title, clause_no, category, content, "
                "effective_from) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, title, clause, cat, content, eff))
