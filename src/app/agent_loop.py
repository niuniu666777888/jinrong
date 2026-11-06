"""Agent 执行循环：决策 → 调工具 → 回填结果 → 再决策，写操作前挂起等人工确认。

设计要点（与交付版 agent_demo 同源，但收敛到 MVP 体量）：

1. 工具注册表：每个工具声明是否属于「写」操作、确认时给用户看什么预览；
2. 三个确认门：建单、提交复核、复核签核，都必须人工点「确认」才执行；
3. 决策可替换：`plan_for(goal)` 是唯一决策点，接真实模型只需换掉它。
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import agent, db
from .errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, CODE_STATE, BizError

MAX_STEPS = 10


# ---------------------------------------------------------------- 工具定义
@dataclass
class Tool:
    name: str
    run: Callable[..., Dict[str, Any]]
    needs_confirm: bool
    label: str
    preview: Callable[[Dict[str, Any]], Dict[str, Any]]


def _p_none(args: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def _p_ticket(args: Dict[str, Any]) -> Dict[str, Any]:
    d = db.q1("SELECT * FROM diff_record WHERE id = ?", (args.get("diff_id", ""),)) or {}
    return {
        "差异": d.get("id"),
        "类型": d.get("diff_type"),
        "金额": round(float(d.get("amount", 0)), 2),
        "处理方式": args.get("handling"),
    }


def _p_ticket_id(args: Dict[str, Any]) -> Dict[str, Any]:
    t = db.q1("SELECT * FROM ticket WHERE id = ?", (args.get("ticket_id", ""),)) or {}
    return {"工单": t.get("ticket_no"), "金额": round(float(t.get("amount", 0)), 2),
            "状态": t.get("status")}


def _tool_search(ctx: Dict[str, Any], query: str) -> Dict[str, Any]:
    from . import retrieval
    hits = retrieval.search(query, 3)
    return {"hits": [{"clause_no": h["clause_no"], "title": h["title"],
                      "score": h["score"]} for h in hits]}


def _tool_query_batch(ctx: Dict[str, Any], channel: str, settle_date: str) -> Dict[str, Any]:
    for row in agent.list_batches():
        if row["channel"] == channel and row["settle_date"] == settle_date:
            return {"batch": row}
    raise BizError(CODE_NOT_FOUND, f"未找到 {channel} 在 {settle_date} 的批次", 404)


TOOLS: Dict[str, Tool] = {
    "search_policy": Tool("search_policy", _tool_search, False, "检索制度条款", _p_none),
    "query_batch": Tool("query_batch", _tool_query_batch, False, "查询对账批次", _p_none),
    "list_diffs": Tool(
        "list_diffs",
        lambda ctx, batch_id: {"items": agent.list_diffs(batch_id)},
        False, "列出批次差异", _p_none),
    "verify_diff": Tool(
        "verify_diff",
        lambda ctx, diff_id: agent.verify_diff(diff_id, ctx["operator"]),
        False, "核验差异并签发凭证", _p_none),
    "create_ticket": Tool(
        "create_ticket",
        lambda ctx, diff_id, token, handling: agent.create_ticket(
            diff_id, token, handling, ctx["operator"]),
        True, "创建差错工单", _p_ticket),
    "submit_ticket": Tool(
        "submit_ticket",
        lambda ctx, ticket_id: agent.submit_ticket(ticket_id, ctx["operator"]),
        True, "提交复核", _p_ticket_id),
    "review_ticket": Tool(
        "review_ticket",
        lambda ctx, ticket_id, approve=True: agent.review_ticket(
            ticket_id, ctx["operator"], approve),
        True, "复核签核", _p_ticket_id),
}


# ---------------------------------------------------------------- 决策
def plan_for(goal: str) -> List[Dict[str, Any]]:
    """把目标翻译成工具链。MVP 用关键词匹配，够用且可解释。

    参数里的 {var} 会在执行时从上下文取值（batch_id / diff_id / token / ticket_id）。
    """
    # 只有出现「动作词」才走处理链；问「是什么/有什么区别」属于只读咨询
    if any(k in goal for k in ("处理", "建单", "提交", "复核", "办一下", "走一遍")):
        return [
            {"tool": "query_batch", "args": {"channel": "微信支付", "settle_date": "2026-11-05"}},
            {"tool": "list_diffs", "args": {"batch_id": "{batch_id}"}},
            {"tool": "verify_diff", "args": {"diff_id": "{diff_id}"}},
            {"tool": "create_ticket",
             "args": {"diff_id": "{diff_id}", "token": "{token}", "handling": "补单"}},
            {"tool": "submit_ticket", "args": {"ticket_id": "{ticket_id}"}},
            {"tool": "review_ticket", "args": {"ticket_id": "{ticket_id}", "approve": True}},
        ]
    return [{"tool": "search_policy", "args": {"query": goal}}]


def _resolve(args: Dict[str, Any], variables: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            out[k] = variables.get(v[1:-1])
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- 状态存取
def _new_run(goal: str, operator: str, plan: List[Dict[str, Any]]) -> str:
    run_id = db.new_id("ar")
    state = {
        "run_id": run_id,
        "goal": goal,
        "operator": operator,
        "plan": plan,
        "cursor": 0,
        "vars": {},
        "events": [],
        "pending": None,
        "status": "running",
    }
    db.execute(
        "INSERT INTO agent_run (id, goal, operator, status, state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, goal, operator, "running", json.dumps(state, ensure_ascii=False),
         db.now_ms(), db.now_ms()),
    )
    return run_id


def _save(state: Dict[str, Any]) -> None:
    db.execute("UPDATE agent_run SET status = ?, state = ?, updated_at = ? WHERE id = ?",
               (state["status"], json.dumps(state, ensure_ascii=False),
                db.now_ms(), state["run_id"]))


def load(run_id: str) -> Dict[str, Any]:
    row = db.q1("SELECT * FROM agent_run WHERE id = ?", (run_id,))
    if not row:
        raise BizError(CODE_NOT_FOUND, "会话不存在", 404)
    return json.loads(row["state"])


def _capture(state: Dict[str, Any], tool: str, result: Dict[str, Any]) -> None:
    v = state["vars"]
    if tool == "query_batch":
        v["batch_id"] = result["batch"]["id"]
    elif tool == "list_diffs":
        items = result.get("items") or []
        if items:
            v["diff_id"] = items[0]["diff_id"]
    elif tool == "verify_diff":
        v["token"] = result.get("token")
    elif tool == "create_ticket":
        v["ticket_id"] = result["ticket_id"]


def _advance(state: Dict[str, Any]) -> Dict[str, Any]:
    """推进循环，直到遇到确认门或走完剧本。"""
    while state["cursor"] < len(state["plan"]):
        if len(state["events"]) >= MAX_STEPS:
            state["status"] = "limit"
            state["pending"] = None
            _save(state)
            return state

        step = state["plan"][state["cursor"]]
        name = step["tool"]
        tool = TOOLS.get(name)
        if tool is None:
            raise BizError(CODE_BAD_REQUEST, f"未知工具 {name}", 400)

        args = _resolve(step["args"], state["vars"])

        if tool.needs_confirm:
            state["pending"] = {
                "tool": name,
                "label": tool.label,
                "args": args,
                "preview": tool.preview(args),
            }
            state["status"] = "waiting_confirm"
            _save(state)
            return state

        result = tool.run({"operator": state["operator"]}, **args)
        _capture(state, name, result)
        state["events"].append({"tool": name, "ok": True, "result": result})
        state["cursor"] += 1

    state["status"] = "done"
    state["pending"] = None
    _save(state)
    return state


# ---------------------------------------------------------------- 对外
def start(goal: str, operator: str) -> Dict[str, Any]:
    run_id = _new_run(goal, operator, plan_for(goal))
    return _advance(load(run_id))


def confirm(run_id: str, operator: str, approved: bool) -> Dict[str, Any]:
    state = load(run_id)
    if state["status"] != "waiting_confirm" or not state["pending"]:
        raise BizError(CODE_STATE, "当前没有待确认的操作", 409)

    pending = state["pending"]

    if not approved:
        state["events"].append({"tool": pending["tool"], "ok": False,
                                "result": {"code": "REJECTED", "message": "人工拒绝"}})
        state["status"] = "aborted"
        state["pending"] = None
        _save(state)
        return state

    tool = TOOLS[pending["tool"]]
    result = tool.run({"operator": operator}, **pending["args"])
    _capture(state, tool.name, result)
    state["events"].append({"tool": tool.name, "ok": True, "result": result})
    state["cursor"] += 1
    state["pending"] = None
    state["status"] = "running"
    _save(state)

    return _advance(state)
