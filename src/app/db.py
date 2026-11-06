"""SQLite 访问层：建表、查询、事务。

并发策略：每次操作开一条短连接（WAL 模式下开销很小），不跨线程共享连接。
MVP 单进程够用；上生产要换 PostgreSQL，见 README「已知边界」。
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy (
  id          TEXT PRIMARY KEY,
  doc         TEXT NOT NULL,
  clause_no   TEXT NOT NULL,
  title       TEXT NOT NULL,
  content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel (
  id    TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  code  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS batch (
  id          TEXT PRIMARY KEY,
  channel_id  TEXT NOT NULL REFERENCES channel(id),
  settle_date TEXT NOT NULL,
  trade_count INTEGER NOT NULL,
  amount      REAL NOT NULL,
  status      TEXT NOT NULL,
  UNIQUE(channel_id, settle_date)
);

CREATE TABLE IF NOT EXISTS diff_record (
  id                TEXT PRIMARY KEY,
  batch_id          TEXT NOT NULL REFERENCES batch(id),
  diff_type         TEXT NOT NULL,          -- 长款 / 短款 / 金额不符
  channel_trade_no  TEXT NOT NULL,
  internal_order_no TEXT NOT NULL,
  amount            REAL NOT NULL,
  occurred_at       INTEGER NOT NULL,       -- 毫秒
  reason            TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'pending'   -- pending / ticketed / closed
);

CREATE TABLE IF NOT EXISTS verify_record (
  token      TEXT PRIMARY KEY,
  diff_id    TEXT NOT NULL REFERENCES diff_record(id),
  operator   TEXT NOT NULL,
  passed     INTEGER NOT NULL,
  issues     TEXT NOT NULL,                 -- JSON 数组
  created_at INTEGER NOT NULL,
  expire_at  INTEGER NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket (
  id           TEXT PRIMARY KEY,
  ticket_no    TEXT NOT NULL UNIQUE,
  diff_id      TEXT NOT NULL UNIQUE REFERENCES diff_record(id),   -- 一笔差异只能建一张单
  diff_type    TEXT NOT NULL,
  handling     TEXT NOT NULL,
  amount       REAL NOT NULL,               -- 服务端重查，不信前端
  status       TEXT NOT NULL,               -- created / submitted / reviewed
  created_by   TEXT NOT NULL,
  reviewed_by  TEXT,
  created_at   INTEGER NOT NULL,
  submitted_at INTEGER
);

CREATE TABLE IF NOT EXISTS ticket_event (
  id         TEXT PRIMARY KEY,
  ticket_id  TEXT NOT NULL REFERENCES ticket(id),
  action     TEXT NOT NULL,
  actor      TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run (
  id         TEXT PRIMARY KEY,
  goal       TEXT NOT NULL,
  operator   TEXT NOT NULL,
  status     TEXT NOT NULL,          -- running / waiting_confirm / done / aborted / limit
  state      TEXT NOT NULL,          -- JSON 快照
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diff_batch ON diff_record(batch_id);
CREATE INDEX IF NOT EXISTS idx_event_ticket ON ticket_event(ticket_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def q(sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]


def q1(sql: str, args: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable[Any] = ()) -> None:
    with _conn() as c:
        c.execute(sql, tuple(args))


def transaction(fn):
    """把多条写语句包进一个事务；fn 收到 connection。"""
    with _conn() as c:
        return fn(c)


def dump_issues(issues: List[Dict[str, Any]]) -> str:
    return json.dumps(issues, ensure_ascii=False)


def load_issues(raw: str) -> List[Dict[str, Any]]:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
