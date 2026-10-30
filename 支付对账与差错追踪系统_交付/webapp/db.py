#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/db.py —— SQLite 持久层
================================================================================
职责：会话、消息、引用来源、知识库切片的读写。

三个设计要点：
  1. 【每操作一个连接】Starlette 会把同步生成器丢进线程池执行，而且
     每次 next() 可能落在不同线程。如果跨 yield 复用一个连接，会撞上
     sqlite3 的 "SQLite objects created in a thread can only be used in that
     same thread"。所以这里每个函数自己开关连接，用完就关。
  2. 【WAL 模式】SSE 是长连接，读请求很密集；WAL 让读不阻塞写，
     再配合 busy_timeout 自动重试写冲突，避免偶发 "database is locked"。
  3. 【时间统一毫秒时间戳】前端直接 new Date(ts) 就能显示，不用做时区换算。
================================================================================
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# 数据库文件位置：默认与 db.py 同目录，启动时可用环境变量覆盖
_DB_PATH: Path = Path(__file__).with_name("app.db")


def configure(path: str) -> None:
    """由 app.py 在启动时调用，指定数据库文件位置。"""
    global _DB_PATH
    _DB_PATH = Path(path)


def now_ms() -> int:
    """当前时间的毫秒时间戳，全库统一用它。"""
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，例如 s_7f3a9c21、m_2b4d8e10。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ==============================================================================
# 连接与建表
# ==============================================================================
def _connect() -> sqlite3.Connection:
    """
    打开一个连接并设置 PRAGMA。

    注意：调用方负责 close()，这里不做连接池——对当前量级（单机、几十并发）
    每次新建连接的开销可以忽略，换来的是彻底避免跨线程复用的问题。
    """
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row          # 让查询结果能按列名取值
    conn.execute("PRAGMA journal_mode=WAL")     # 读写并发
    conn.execute("PRAGMA busy_timeout=5000")    # 写冲突最多等 5 秒，而不是立刻报错
    conn.execute("PRAGMA foreign_keys=ON")      # 外键约束生效
    return conn


SCHEMA = """
-- 会话：一个浏览器标签页/一次咨询对应一条
CREATE TABLE IF NOT EXISTS session (
  id          TEXT PRIMARY KEY,
  employee_id TEXT,                                   -- 预留：接 SSO 后填工号
  title       TEXT NOT NULL DEFAULT '新会话',
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- 消息：user / assistant 各一行
CREATE TABLE IF NOT EXISTS message (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content    TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'ok',              -- ok / error / aborted
  error_code INTEGER,
  created_at INTEGER NOT NULL
);

-- 引用来源：一条助手回答对应多条切片
CREATE TABLE IF NOT EXISTS message_source (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  chunk_id     TEXT NOT NULL,
  file_name    TEXT NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset   INTEGER NOT NULL,
  score        REAL NOT NULL,
  snippet      TEXT NOT NULL
);

-- 知识库切片：阶段 1 每次启动重建的内存数组，这里固化下来，引用才能永久回溯
CREATE TABLE IF NOT EXISTS chunk (
  id           TEXT PRIMARY KEY,
  doc_id       TEXT NOT NULL,
  file_name    TEXT NOT NULL,
  ordinal      INTEGER NOT NULL,                      -- 该文件内的第几片
  start_offset INTEGER NOT NULL,                      -- 原文起止，精确到字
  end_offset   INTEGER NOT NULL,
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_session ON message(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_source_message  ON message_source(message_id);
CREATE INDEX IF NOT EXISTS idx_chunk_doc       ON chunk(doc_id, ordinal);
"""


# 二期表：账号、文档、提示词、审计标记。
# 单独一个脚本，是为了让一期建的表不用改——只做追加。
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS app_user (
  id            TEXT PRIMARY KEY,
  employee_no   TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('employee', 'admin')),
  password_hash TEXT NOT NULL,
  token_version INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    INTEGER NOT NULL,
  last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS doc (
  id          TEXT PRIMARY KEY,
  file_name   TEXT NOT NULL,
  file_type   TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  sha256      TEXT NOT NULL UNIQUE,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL,
  error_msg   TEXT,
  uploaded_by TEXT,
  created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_template (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  content    TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,
  is_active  INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_version (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL,
  version     INTEGER NOT NULL,
  content     TEXT NOT NULL,
  created_by  TEXT,
  created_at  INTEGER NOT NULL,
  UNIQUE (template_id, version)
);

CREATE TABLE IF NOT EXISTS audit_flag (
  message_id TEXT PRIMARY KEY,
  category   TEXT NOT NULL,
  reason     TEXT,
  flagged_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_status ON doc(status, created_at);
CREATE INDEX IF NOT EXISTS idx_user_no    ON app_user(employee_no);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """
    轻量迁移：给已有库补新列。

    为什么不用 Alembic：这个项目只有一个表需要加列，
    引入迁移框架的维护成本远大于收益。PRAGMA 查一下有没有，没有就 ALTER。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
    if "prompt_version" not in cols:
        # 审计要用：回答当时用的是第几版提示词，回滚后还能解释历史回答
        conn.execute("ALTER TABLE message ADD COLUMN prompt_version INTEGER")


def init_db() -> None:
    """建表 + 迁移。可重复执行。"""
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA_V2)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# 会话
# ==============================================================================
def create_session(employee_id: Optional[str] = None) -> Dict[str, Any]:
    """新建会话，返回 {id, title, created_at}。"""
    sid = new_id("s")
    ts = now_ms()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO session (id, employee_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, employee_id, "新会话", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return {"session_id": sid, "title": "新会话", "created_at": ts}


def ensure_session(session_id: str, employee_id: Optional[str] = None) -> Dict[str, Any]:
    """
    确保会话存在：不存在就用前端传来的 ID 建一条。

    为什么不让前端先调 POST /sessions 拿 ID：
      用户打开页面就问，多一次往返就多一次可能失败的环节；
      用前端本地生成的 ID 直接建，断网重连后 ID 也不会变。
    """
    existing = get_session(session_id)
    if existing:
        return existing

    ts = now_ms()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO session (id, employee_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, employee_id, "新会话", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id, "title": "新会话", "created_at": ts}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 查会话，不存在返回 None。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_session(session_id: str, title: Optional[str] = None) -> None:
    """
    更新会话的活跃时间；首次提问时顺便用问题前 20 字生成标题，
    这样历史列表里不会全是"新会话"。
    """
    conn = _connect()
    try:
        if title:
            conn.execute(
                "UPDATE session SET updated_at = ?, title = ? "
                "WHERE id = ? AND (title = '新会话' OR title IS NULL)",
                (now_ms(), title, session_id),
            )
        conn.execute("UPDATE session SET updated_at = ? WHERE id = ?", (now_ms(), session_id))
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# 消息
# ==============================================================================
def add_message(session_id: str, role: str, content: str,
                status: str = "ok", error_code: Optional[int] = None) -> str:
    """写一条消息，返回消息 ID。"""
    mid = new_id("m")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO message (id, session_id, role, content, status, error_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, session_id, role, content, status, error_code, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()
    return mid


def add_message_with_id(message_id: str, session_id: str, role: str, content: str,
                        status: str = "ok", error_code: Optional[int] = None,
                        prompt_version: Optional[int] = None) -> str:
    """
    按指定 ID 写消息。

    流式接口要用它：meta 事件里已经把 message_id 推给前端了，
    如果最后再让数据库生成一个新 ID，前端的消息 ID 和库里的就对不上，
    刷新页面后会出现"消息重复"或"引用挂错"。

    prompt_version 记录这次回答用的提示词版本，审计时能解释
    "为什么同一问题，回滚前后的回答不一样"。
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO message (id, session_id, role, content, status, error_code, "
            "created_at, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, session_id, role, content, status, error_code,
             now_ms(), prompt_version),
        )
        conn.commit()
    finally:
        conn.close()
    return message_id


def update_message(message_id: str, content: str,
                   status: str = "ok", error_code: Optional[int] = None) -> None:
    """回填助手回答的最终内容（流式生成结束、中断或失败时调用）。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE message SET content = ?, status = ?, error_code = ? WHERE id = ?",
            (content, status, error_code, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def count_messages(session_id: str) -> int:
    """统计会话内的消息条数，用于判断是否超过上限。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM message WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def recent_messages(session_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    """
    取最近 N 条消息（用于拼上下文），按时间正序返回。

    只取 status='ok'：失败或中断的半截回答进上下文只会污染模型。
    注意用子查询先倒序取 N 条、再正序排列，否则直接 ORDER BY + LIMIT
    拿到的是"最早 N 条"，不是"最近 N 条"。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT rowid AS _rid, * FROM message "
            "  WHERE session_id = ? AND status = 'ok' "
            "  ORDER BY created_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY created_at ASC, _rid ASC",
            (session_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item.pop("_rid", None)      # 仅用于排序，不对外暴露
            result.append(item)
        return result
    finally:
        conn.close()


def list_messages(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """列出会话的最近消息（含引用），用于刷新页面后恢复对话。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT rowid AS _rid, * FROM message WHERE session_id = ? "
            "  ORDER BY created_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY created_at ASC, _rid ASC",
            (session_id, limit),
        ).fetchall()

        result: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item.pop("_rid", None)      # 仅用于排序，不对外暴露
            # 僵尸行兜底：进程没机会收尾时，'streaming' 会一直挂着。
            # 一次正常回答不可能超过 60 秒（底层有超时控制），超时即视为中断。
            if (item["status"] == "streaming"
                    and now_ms() - item["created_at"] > 60000):
                item["status"] = "aborted"
            item["sources"] = get_sources(r["id"])
            result.append(item)
        return result
    finally:
        conn.close()


# ==============================================================================
# 引用来源
# ==============================================================================
def add_sources(message_id: str, sources: List[Dict[str, Any]]) -> None:
    """批量写入一条回答的引用来源。"""
    if not sources:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO message_source "
            "(message_id, chunk_id, file_name, start_offset, end_offset, score, snippet) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (message_id, s["chunk_id"], s["file_name"], s["start"],
                 s["end"], float(s["score"]), s["snippet"])
                for s in sources
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_sources(message_id: str) -> List[Dict[str, Any]]:
    """取一条回答的全部引用，按分数从高到低。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM message_source WHERE message_id = ? ORDER BY score DESC",
            (message_id,),
        ).fetchall()
        return [
            {
                "chunk_id": r["chunk_id"],
                "file_name": r["file_name"],
                "start": r["start_offset"],
                "end": r["end_offset"],
                "score": round(float(r["score"]), 3),
                "snippet": r["snippet"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ==============================================================================
# 知识库切片
# ==============================================================================
def replace_chunks(items: List[Dict[str, Any]]) -> None:
    """重建切片表（启动时调用一次，先清空再写入，保证与文档一致）。"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM chunk")
        conn.executemany(
            "INSERT INTO chunk "
            "(id, doc_id, file_name, ordinal, start_offset, end_offset, content, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (c["chunk_id"], c["doc_id"], c["file_name"], c["ordinal"],
                 c["start"], c["end"], c["content"], c["content_hash"])
                for c in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_chunk(chunk_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 取切片原文，供前端"查看原文"用。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM chunk WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        return {
            "chunk_id": row["id"],
            "doc_id": row["doc_id"],
            "file_name": row["file_name"],
            "start": row["start_offset"],
            "end": row["end_offset"],
            "content": row["content"],
        }
    finally:
        conn.close()


def chunk_count() -> int:
    """切片总数，用于健康检查。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM chunk").fetchone()
        return int(row["c"])
    finally:
        conn.close()


# ==============================================================================
# 流式过程中的增量落库
# ==============================================================================
def flush_message(message_id: str, content: str) -> None:
    """
    流式过程中把"已生成的部分"刷进库，但不改状态。

    用途：连接突然断掉时，至少能查到断线前的内容；
    代价是每 8 段写一次库，量很小（WAL 模式下写不阻塞读）。
    """
    conn = _connect()
    try:
        conn.execute("UPDATE message SET content = ? WHERE id = ?", (content, message_id))
        conn.commit()
    finally:
        conn.close()


def finalize_message(message_id: str, content: str,
                     status: str = "ok", error_code: Optional[int] = None) -> None:
    """定稿：写入最终内容与状态（ok / aborted / error）。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE message SET content = ?, status = ?, error_code = ? WHERE id = ?",
            (content, status, error_code, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def abort_stale_messages() -> int:
    """
    启动时清理：把上次没走完的 streaming 行标记为 aborted。

    为什么需要：进程被 kill、机器重启、部署直接换容器，
    都不可能执行到收尾逻辑，库里会留一堆 status='streaming' 的僵尸行。
    不清理的话，前端会一直显示"正在生成"，而实际上早就没有进程在写了。
    """
    conn = _connect()
    try:
        cur = conn.execute("UPDATE message SET status = 'aborted' WHERE status = 'streaming'")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ==============================================================================
# 二阶段新增：切片批量写入 / 全量读取 / 会话归属绑定
# ==============================================================================
def insert_chunks(items: List[Dict[str, Any]]) -> None:
    """
    批量写入切片。

    用 INSERT OR REPLACE：同一 doc_id + ordinal 重复写入时覆盖，
    重传同一份文档不会产生重复切片（虽然上传接口已用 sha256 挡了一层）。
    """
    if not items:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO chunk "
            "(id, doc_id, file_name, ordinal, start_offset, end_offset, content, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (c["chunk_id"], c["doc_id"], c["file_name"], c["ordinal"],
                 c["start"], c["end"], c["content"], c["content_hash"])
                for c in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def all_chunks() -> List[Dict[str, Any]]:
    """
    读出全部切片，用于重建索引。

    排序固定为 (doc_id, ordinal)：保证每次重建得到的切片顺序一致，
    这样 hit 的下标和 STATE["chunks"] 的下标才能对得上。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chunk ORDER BY doc_id, ordinal"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bind_session(session_id: str, user_id: str) -> None:
    """
    给迁移前的匿名会话补上归属。

    只在该字段为空时写入，不覆盖已有归属——
    否则任何人拿到别人的 session_id 提问，就把别人的会话"认领"走了。
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE session SET employee_id = ? "
            "WHERE id = ? AND (employee_id IS NULL OR employee_id = '')",
            (user_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# 数据保留：按保留期清理历史会话
# ==============================================================================
def count_assistant_since(ts_ms: int) -> int:
    """统计某时间点之后产生的助手回答数，用于 LLM 日配额判断。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM message WHERE role = 'assistant' AND created_at >= ?",
            (ts_ms,),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def purge_old_data(retention_days: int) -> Dict[str, int]:
    """
    删除超过保留期的消息与会话。

    等保要求审计日志至少留 6 个月，所以默认 180 天，且只删“过期”的，
    不做抽样保留——审计要能回答“三个月前那笔是谁处理的”，删了就没法答。
    message_source 有 ON DELETE CASCADE，引用会跟着删干净，不会留孤儿行。

    返回删除条数，供启动日志打印。
    """
    if retention_days <= 0:
        return {"messages": 0, "sessions": 0}

    cutoff = now_ms() - retention_days * 86400_000
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM message WHERE created_at < ?", (cutoff,))
        msgs = cur.rowcount
        cur2 = conn.execute(
            "DELETE FROM session WHERE updated_at < ? "
            "AND id NOT IN (SELECT DISTINCT session_id FROM message)",
            (cutoff,),
        )
        sessions = cur2.rowcount
        conn.commit()
        return {"messages": int(msgs), "sessions": int(sessions)}
    finally:
        conn.close()
