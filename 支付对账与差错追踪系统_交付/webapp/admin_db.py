#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/admin_db.py —— 管理后台的数据访问层
================================================================================
和 db.py 分开的理由：db.py 是"聊天链路"的数据（会话、消息、引用、切片），
这里全是"后台管理"的数据（账号、文档、提示词、审计标记）。
两者边界清楚，合在一起会变成 900 行的巨文件。

连接策略与 db.py 完全一致：每个函数自己开关短连接，避免跨线程复用。
================================================================================
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from db import _connect, new_id, now_ms

# ==============================================================================
# 账号
# ==============================================================================
def count_users() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) c FROM app_user").fetchone()["c"])
    finally:
        conn.close()


def create_user(employee_no: str, name: str, role: str, password_hash: str) -> str:
    """建账号。工号重复会抛 sqlite3.IntegrityError，由调用方转成业务错误。"""
    uid = new_id("u")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO app_user (id, employee_no, name, role, password_hash, "
            "token_version, status, created_at) VALUES (?, ?, ?, ?, ?, 1, 'active', ?)",
            (uid, employee_no, name, role, password_hash, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()
    return uid


def get_user_by_no(employee_no: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM app_user WHERE employee_no = ?",
                           (employee_no,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_login(user_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE app_user SET last_login_at = ? WHERE id = ?",
                     (now_ms(), user_id))
        conn.commit()
    finally:
        conn.close()


def change_password(user_id: str, password_hash: str) -> None:
    """
    改密的同时把 token_version +1。

    这样所有已签发的旧令牌立即失效——不用维护黑名单表，
    校验时比对 token 里的 ver 和库里的 token_version 即可。
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE app_user SET password_hash = ?, token_version = token_version + 1 "
            "WHERE id = ?",
            (password_hash, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, employee_no, name, role, status, created_at, last_login_at "
            "FROM app_user ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# 文档
# ==============================================================================
def get_doc_by_sha(sha256: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM doc WHERE sha256 = ?", (sha256,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_doc(doc_id: str, file_name: str, file_type: str, size_bytes: int,
               sha256: str, uploaded_by: Optional[str]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO doc (id, file_name, file_type, size_bytes, sha256, "
            "chunk_count, status, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 'indexing', ?, ?)",
            (doc_id, file_name, file_type, size_bytes, sha256, uploaded_by, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def finish_doc(doc_id: str, chunk_count: int, status: str = "indexed",
               error_msg: Optional[str] = None) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE doc SET chunk_count = ?, status = ?, error_msg = ? WHERE id = ?",
            (chunk_count, status, error_msg, doc_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_docs(keyword: str = "", status: str = "",
              page: int = 1, size: int = 20) -> Tuple[List[Dict[str, Any]], int]:
    """分页查文档列表。keyword 模糊匹配文件名，status 精确匹配。"""
    where, params = ["1=1"], []
    if keyword:
        where.append("d.file_name LIKE ?")
        params.append(f"%{keyword}%")
    if status:
        where.append("d.status = ?")
        params.append(status)

    cond = " AND ".join(where)
    conn = _connect()
    try:
        total = int(conn.execute(
            f"SELECT COUNT(*) c FROM doc d WHERE {cond}", params
        ).fetchone()["c"])

        rows = conn.execute(
            f"SELECT d.*, u.name AS uploader_name FROM doc d "
            f"LEFT JOIN app_user u ON u.id = d.uploaded_by "
            f"WHERE {cond} ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()

        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_doc(doc_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM doc WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_doc(doc_id: str) -> int:
    """删除文档及其全部切片，返回删掉的切片数。"""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM chunk WHERE doc_id = ?", (doc_id,))
        n = cur.rowcount
        conn.execute("DELETE FROM doc WHERE id = ?", (doc_id,))
        conn.commit()
        return n
    finally:
        conn.close()


def chunks_of_doc(doc_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """预览某文档的切片，用于排查"为什么搜不到"。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chunk WHERE doc_id = ? ORDER BY ordinal LIMIT ?",
            (doc_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# 提示词模板
# ==============================================================================
def get_prompt_by_name(name: str = "system_default") -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM prompt_template WHERE name = ?",
                           (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_prompt(name: str, content: str) -> None:
    """首次启动时写入默认模板；已存在就不动，避免覆盖管理员的修改。"""
    conn = _connect()
    try:
        exists = conn.execute("SELECT 1 FROM prompt_template WHERE name = ?",
                              (name,)).fetchone()
        if not exists:
            tid = new_id("pt")
            ts = now_ms()
            conn.execute(
                "INSERT INTO prompt_template (id, name, content, version, is_active, "
                "updated_by, updated_at) VALUES (?, ?, ?, 1, 1, 'system', ?)",
                (tid, name, content, ts),
            )
            conn.execute(
                "INSERT INTO prompt_version (template_id, version, content, created_by, "
                "created_at) VALUES (?, 1, ?, 'system', ?)",
                (tid, content, ts),
            )
        conn.commit()
    finally:
        conn.close()


def save_prompt(template_id: str, content: str, user_id: str) -> Dict[str, Any]:
    """
    保存模板：版本 +1，同时写一条历史。

    为什么历史不能覆盖：回滚要能回到任意一版；审计要能查到
    "第 3 版是什么内容"，覆盖掉就永远查不到了。
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT version FROM prompt_template WHERE id = ?",
                           (template_id,)).fetchone()
        if not row:
            raise KeyError(template_id)

        new_version = int(row["version"]) + 1
        ts = now_ms()

        conn.execute(
            "UPDATE prompt_template SET content = ?, version = ?, updated_by = ?, "
            "updated_at = ? WHERE id = ?",
            (content, new_version, user_id, ts, template_id),
        )
        conn.execute(
            "INSERT INTO prompt_version (template_id, version, content, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (template_id, new_version, content, user_id, ts),
        )
        conn.commit()
        return {"version": new_version, "updated_at": ts}
    finally:
        conn.close()


def list_prompt_versions(template_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT v.*, u.name AS author_name FROM prompt_version v "
            "LEFT JOIN app_user u ON u.id = v.created_by "
            "WHERE v.template_id = ? ORDER BY v.version DESC LIMIT ?",
            (template_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_prompt_version(template_id: str, version: int) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM prompt_version WHERE template_id = ? AND version = ?",
            (template_id, version),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==============================================================================
# 审计
# ==============================================================================
def _audit_where(f: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    把筛选条件翻译成 SQL。

    统一写在这里，是为了"列表"和"导出"用完全相同的条件——
    两边各写一遍，迟早出现"看到的和导出的不一致"。
    """
    where, params = ["m.role = 'assistant'"], []

    if f.get("employee_no"):
        where.append("u.employee_no = ?")
        params.append(f["employee_no"])
    if f.get("from_ms"):
        where.append("m.created_at >= ?")
        params.append(int(f["from_ms"]))
    if f.get("to_ms"):
        where.append("m.created_at <= ?")
        params.append(int(f["to_ms"]))
    if f.get("keyword"):
        where.append("(m.content LIKE ? OR EXISTS ("
                     "SELECT 1 FROM message q WHERE q.session_id = m.session_id "
                     "AND q.role = 'user' AND q.created_at <= m.created_at "
                     "AND q.content LIKE ?))")
        kw = f"%{f['keyword']}%"
        params.extend([kw, kw])
    if f.get("flagged") == "yes":
        where.append("f.message_id IS NOT NULL")
    elif f.get("flagged") == "no":
        where.append("f.message_id IS NULL")

    return " AND ".join(where), params


def audit_page(f: Dict[str, Any], page: int = 1,
               size: int = 20) -> Tuple[List[Dict[str, Any]], int]:
    """
    分页查审计记录。

    每条记录 = 一条助手回答 + 它对应的问题。
    问题用"同一会话里紧挨着的前一条用户消息"来关联——
    比在 message 表加 parent_id 简单，也不会破坏已有数据结构。
    """
    cond, params = _audit_where(f)
    conn = _connect()
    try:
        total = int(conn.execute(
            f"SELECT COUNT(*) c FROM message m "
            f"JOIN session s ON s.id = m.session_id "
            f"LEFT JOIN app_user u ON u.id = s.employee_id "
            f"LEFT JOIN audit_flag f ON f.message_id = m.id "
            f"WHERE {cond}", params,
        ).fetchone()["c"])

        rows = conn.execute(
            f"SELECT m.id, m.session_id, m.content, m.status, m.created_at, "
            f"m.prompt_version, s.employee_id, u.name AS employee_name, "
            f"u.employee_no, f.category, f.reason, f.created_at AS flagged_at "
            f"FROM message m "
            f"JOIN session s ON s.id = m.session_id "
            f"LEFT JOIN app_user u ON u.id = s.employee_id "
            f"LEFT JOIN audit_flag f ON f.message_id = m.id "
            f"WHERE {cond} ORDER BY m.created_at DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()

        items: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["question"] = prev_question(r["session_id"], r["created_at"]) or "（无）"
            items.append(item)
        return items, total
    finally:
        conn.close()


def prev_question(session_id: str, before_ts: int) -> str:
    """取同一会话中、早于该回答的最近一条用户消息。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT content FROM message WHERE session_id = ? AND role = 'user' "
            "AND created_at <= ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id, before_ts),
        ).fetchone()
        return row["content"] if row else ""
    finally:
        conn.close()


def get_audit_item(message_id: str) -> Optional[Dict[str, Any]]:
    """单条详审：回答 + 问题 + 引用 + 标记。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT m.*, s.employee_id, u.name AS employee_name, u.employee_no, "
            "f.category, f.reason, f.flagged_by, f.created_at AS flagged_at "
            "FROM message m "
            "JOIN session s ON s.id = m.session_id "
            "LEFT JOIN app_user u ON u.id = s.employee_id "
            "LEFT JOIN audit_flag f ON f.message_id = m.id "
            "WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        if not row:
            return None

        item = dict(row)
        item["question"] = prev_question(row["session_id"], row["created_at"])
        item["sources"] = [
            {
                "file_name": s["file_name"],
                "start": s["start_offset"],
                "end": s["end_offset"],
                "score": round(float(s["score"]), 3),
                "snippet": s["snippet"],
            }
            for s in conn.execute(
                "SELECT * FROM message_source WHERE message_id = ? ORDER BY score DESC",
                (message_id,),
            ).fetchall()
        ]
        return item
    finally:
        conn.close()


def flag_message(message_id: str, category: str, reason: str, user_id: str) -> None:
    """标记疑似违规。用 REPLACE 实现"重复标记覆盖旧的"，避免一条消息挂多条标记。"""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO audit_flag (message_id, category, reason, "
            "flagged_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, category, reason, user_id, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def unflag_message(message_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM audit_flag WHERE message_id = ?", (message_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def audit_for_export(f: Dict[str, Any], limit: int = 5000) -> List[Dict[str, Any]]:
    """导出用：同样的筛选条件，不分页，但有硬上限，防止一键拖垮数据库。"""
    cond, params = _audit_where(f)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT m.id, m.content, m.created_at, m.prompt_version, "
            f"u.employee_no, u.name AS employee_name, f.category, f.reason "
            f"FROM message m "
            f"JOIN session s ON s.id = m.session_id "
            f"LEFT JOIN app_user u ON u.id = s.employee_id "
            f"LEFT JOIN audit_flag f ON f.message_id = m.id "
            f"WHERE {cond} ORDER BY m.created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        out = []
        for r in rows:
            item = dict(r)
            item["question"] = prev_question(r["session_id"], r["created_at"]) if "session_id" in r.keys() else ""
            out.append(item)
        return out
    finally:
        conn.close()


# ==============================================================================
# 概览统计
# ==============================================================================
def dashboard_stats(today_start_ms: int) -> Dict[str, Any]:
    conn = _connect()
    try:
        one = lambda sql, args=(): int(conn.execute(sql, args).fetchone()["c"])

        return {
            "docs": one("SELECT COUNT(*) c FROM doc WHERE status = 'indexed'"),
            "chunks": one("SELECT COUNT(*) c FROM chunk"),
            "messages_today": one(
                "SELECT COUNT(*) c FROM message WHERE role = 'assistant' AND created_at >= ?",
                (today_start_ms,)),
            "flagged_today": one(
                "SELECT COUNT(*) c FROM audit_flag WHERE created_at >= ?",
                (today_start_ms,)),
            "flagged_total": one("SELECT COUNT(*) c FROM audit_flag"),
            "users": one("SELECT COUNT(*) c FROM app_user"),
        }
    finally:
        conn.close()
