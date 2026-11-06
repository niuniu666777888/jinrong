"""差错处理核心：核验 → 签发凭证 → 建单 → 提交 → 复核。

三道防线：
1. 写接口不收金额，金额一律从 diff_record 重查；
2. 建单必须持有效凭证，凭证是 HMAC 签名的，且库里有过核验记录；
3. 复核人不能等于建单人（四眼原则），在服务端拒绝。
"""

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config, db
from .errors import (CODE_BAD_REQUEST, CODE_FORBIDDEN, CODE_NOT_FOUND,
                     CODE_STATE, BizError)

DAY_MS = 86_400_000

HANDLING_BY_TYPE: Dict[str, List[str]] = {
    "长款": ["补单", "退款"],
    "短款": ["调单", "补偿扣款"],
    "金额不符": ["订正"],
}

# 单进程保护：MVP 里用一把锁把「读状态 → 写状态」串起来。
# 多进程部署必须依赖数据库约束（ticket.diff_id UNIQUE 已经在 schema 里）。
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 凭证
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64(raw)
    mac = hmac.new(config.SECRET.encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).hexdigest()[:32]
    return f"vt_{body}.{mac}"


def _verify_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or not token.startswith("vt_") or "." not in token:
        return None
    body, mac = token[3:].split(".", 1)
    expect = hmac.new(config.SECRET.encode("utf-8"), body.encode("ascii"),
                      hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expect):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (json.JSONDecodeError, ValueError):
        return None
    return payload


# ---------------------------------------------------------------- 核验
def verify_diff(diff_id: str, operator: str) -> Dict[str, Any]:
    d = db.q1("SELECT * FROM diff_record WHERE id = ?", (diff_id,))
    if not d:
        raise BizError(CODE_NOT_FOUND, f"差异 {diff_id} 不存在", 404)
    if d["status"] != "pending":
        raise BizError(CODE_STATE, f"该差异已{d['status']}，不能重复核验", 409)

    issues: List[Dict[str, str]] = []
    age_days = (db.now_ms() - d["occurred_at"]) / DAY_MS
    if age_days > config.LOOKBACK_DAYS:
        issues.append({
            "level": "error", "rule": "OUT_OF_WINDOW",
            "message": f"差异已产生 {age_days:.1f} 天，超过 {config.LOOKBACK_DAYS} 天回溯窗口，"
                       f"须转资金风险经理评估",
        })
    if d["amount"] > config.HIGH_AMOUNT:
        issues.append({
            "level": "warning", "rule": "HIGH_AMOUNT",
            "message": f"单笔 {d['amount']:.2f} 元超过 {config.HIGH_AMOUNT:.0f} 元，"
                       f"建单须标注风险等级并抄送风险经理",
        })
    if d["amount"] <= 0:
        issues.append({"level": "error", "rule": "BAD_AMOUNT", "message": "差异金额非法"})

    passed = not any(i["level"] == "error" for i in issues)
    now = db.now_ms()
    token = ""
    if passed:
        token = _sign({"diff": diff_id, "op": operator, "exp": now + config.TOKEN_TTL_SECONDS * 1000})
    rec_token = token or f"vt_failed_{db.new_id('x')}"

    db.execute(
        "INSERT INTO verify_record (token, diff_id, operator, passed, issues, "
        "created_at, expire_at, used) VALUES (?,?,?,?,?,?,?,0)",
        (rec_token, diff_id, operator, 1 if passed else 0, db.dump_issues(issues),
         now, now + config.TOKEN_TTL_SECONDS * 1000),
    )
    return {
        "diff": _diff_out(d),
        "passed": passed,
        "issues": issues,
        "token": token or None,
        "expires_in": config.TOKEN_TTL_SECONDS if passed else 0,
    }


# ---------------------------------------------------------------- 建单
def create_ticket(diff_id: str, token: str, handling: str, operator: str) -> Dict[str, Any]:
    with _WRITE_LOCK:
        d = db.q1("SELECT * FROM diff_record WHERE id = ?", (diff_id,))
        if not d:
            raise BizError(CODE_NOT_FOUND, f"差异 {diff_id} 不存在", 404)

        allowed = HANDLING_BY_TYPE.get(d["diff_type"], [])
        if handling not in allowed:
            raise BizError(CODE_BAD_REQUEST,
                           f"{d['diff_type']} 的处理方式只能是 {'/'.join(allowed)}", 400)

        payload = _verify_token(token)
        if not payload:
            raise BizError(CODE_BAD_REQUEST, "核验凭证无效，请重新核验差异后再建单", 400)
        if payload.get("diff") != diff_id:
            raise BizError(CODE_BAD_REQUEST, "核验凭证与当前差异不匹配", 400)
        if payload.get("exp", 0) < db.now_ms():
            raise BizError(CODE_BAD_REQUEST, "核验凭证已过期，请重新核验", 400)

        rec = db.q1("SELECT * FROM verify_record WHERE token = ?", (token,))
        if not rec or not rec["passed"]:
            raise BizError(CODE_STATE, "该差异未通过核验，不能建单", 409)
        if rec["used"]:
            raise BizError(CODE_STATE, "该凭证已使用过，不能重复建单", 409)
        if rec["expire_at"] < db.now_ms():
            raise BizError(CODE_STATE, "核验已过期（超过有效时长），请重新核验", 409)
        if d["status"] != "pending":
            raise BizError(CODE_STATE, f"该差异已{d['status']}，不能重复建单", 409)

        now = db.now_ms()
        ticket_id = db.new_id("t")
        ticket_no = "CT" + time.strftime("%Y%m%d") + "-" + ticket_id[-4:].upper()
        try:
            db.execute(
                "INSERT INTO ticket (id, ticket_no, diff_id, diff_type, handling, amount, "
                "status, created_by, created_at) VALUES (?,?,?,?,?,?,'created',?,?)",
                (ticket_id, ticket_no, diff_id, d["diff_type"], handling, d["amount"],
                 operator, now),
            )
        except sqlite3.IntegrityError:
            raise BizError(CODE_STATE, "该差异已建单，不能重复创建", 409)

        db.execute("UPDATE diff_record SET status = 'ticketed' WHERE id = ?", (diff_id,))
        db.execute("UPDATE verify_record SET used = 1 WHERE token = ?", (token,))
        _event(ticket_id, "create", operator,
               f"{d['diff_type']} {d['amount']:.2f} 元 / {handling}")
        return get_ticket(ticket_id)


# ---------------------------------------------------------------- 提交 / 复核
def submit_ticket(ticket_id: str, operator: str) -> Dict[str, Any]:
    with _WRITE_LOCK:
        t = db.q1("SELECT * FROM ticket WHERE id = ?", (ticket_id,))
        if not t:
            raise BizError(CODE_NOT_FOUND, "工单不存在", 404)
        if t["status"] != "created":
            raise BizError(CODE_STATE, f"工单状态为 {t['status']}，不能重复提交", 409)
        db.execute("UPDATE ticket SET status = 'submitted', submitted_at = ? WHERE id = ?",
                   (db.now_ms(), ticket_id))
        _event(ticket_id, "submit", operator, "提交复核")
        return get_ticket(ticket_id)


def review_ticket(ticket_id: str, operator: str, approve: bool = True) -> Dict[str, Any]:
    """复核签核。四眼原则在这里强制：复核人不得等于建单人。"""
    with _WRITE_LOCK:
        t = db.q1("SELECT * FROM ticket WHERE id = ?", (ticket_id,))
        if not t:
            raise BizError(CODE_NOT_FOUND, "工单不存在", 404)
        if t["status"] != "submitted":
            raise BizError(CODE_STATE, f"工单状态为 {t['status']}，不能复核", 409)
        if operator == t["created_by"]:
            raise BizError(CODE_FORBIDDEN, "建单人与复核人不能是同一人（双人复核）", 403)
        if not approve:
            db.execute("UPDATE ticket SET status = 'created', reviewed_by = NULL, "
                       "submitted_at = NULL WHERE id = ?", (ticket_id,))
            _event(ticket_id, "reject", operator, "退回建单人")
            return get_ticket(ticket_id)

        db.execute("UPDATE ticket SET status = 'reviewed', reviewed_by = ? WHERE id = ?",
                   (operator, ticket_id))
        db.execute("UPDATE diff_record SET status = 'closed' WHERE id = ?", (t["diff_id"],))
        _event(ticket_id, "review", operator, "复核通过")
        return get_ticket(ticket_id)


# ---------------------------------------------------------------- 查询
def _event(ticket_id: str, action: str, actor: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO ticket_event (id, ticket_id, action, actor, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (db.new_id("e"), ticket_id, action, actor, detail, db.now_ms()),
    )


def _diff_out(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "diff_id": d["id"],
        "diff_type": d["diff_type"],
        "amount": round(d["amount"], 2),
        "channel_trade_no": d["channel_trade_no"],
        "internal_order_no": d["internal_order_no"],
        "occurred_at": d["occurred_at"],
        "age_days": round((db.now_ms() - d["occurred_at"]) / DAY_MS, 1),
        "reason": d["reason"],
        "status": d["status"],
    }


def diff_out(d: Dict[str, Any]) -> Dict[str, Any]:
    return _diff_out(d)


def list_batches() -> List[Dict[str, Any]]:
    return db.q(
        "SELECT b.id, c.name AS channel, b.settle_date, b.trade_count, b.amount, b.status, "
        "(SELECT COUNT(*) FROM diff_record d WHERE d.batch_id = b.id) AS diff_count "
        "FROM batch b JOIN channel c ON c.id = b.channel_id ORDER BY b.settle_date DESC, c.name"
    )


def list_diffs(batch_id: str = "") -> List[Dict[str, Any]]:
    sql = ("SELECT d.*, b.settle_date FROM diff_record d JOIN batch b ON b.id = d.batch_id "
           + ("WHERE d.batch_id = ? " if batch_id else "")
           + "ORDER BY d.occurred_at DESC")
    rows = db.q(sql, (batch_id,) if batch_id else ())
    return [diff_out(r) | {"settle_date": r["settle_date"]} for r in rows]


def get_ticket(ticket_id: str) -> Dict[str, Any]:
    t = db.q1("SELECT * FROM ticket WHERE id = ?", (ticket_id,))
    if not t:
        raise BizError(CODE_NOT_FOUND, "工单不存在", 404)
    events = db.q("SELECT action, actor, detail, created_at FROM ticket_event "
                  "WHERE ticket_id = ? ORDER BY created_at", (ticket_id,))
    return {
        "ticket_id": t["id"],
        "ticket_no": t["ticket_no"],
        "diff_id": t["diff_id"],
        "diff_type": t["diff_type"],
        "handling": t["handling"],
        "amount": round(t["amount"], 2),
        "status": t["status"],
        "created_by": t["created_by"],
        "reviewed_by": t["reviewed_by"],
        "created_at": t["created_at"],
        "events": events,
    }


def list_tickets() -> List[Dict[str, Any]]:
    return [get_ticket(r["id"]) for r in db.q("SELECT id FROM ticket ORDER BY created_at DESC")]
