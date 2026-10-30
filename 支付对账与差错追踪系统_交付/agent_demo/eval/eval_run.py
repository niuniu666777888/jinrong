#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/eval/eval_run.py —— 30 条评测用例的执行器
================================================================================
用真实系统跑评测，而不是"人工看一眼觉得还行"。

可自动判定的三类：
  policy     检索命中/未命中 —— 陷阱题必须检索不到，正常题必须命中指定条款
  tool       按期望顺序真实执行工具链 —— 顺序错了会报错，链断了会失败
  violation  用"越权请求"去打系统 —— 必须被挡住（返回错误码或没有对应工具）

不可自动判定的：
  context    代词指代需要真实模型 + 多轮会话，离线剧本测不了
             这类标 MANUAL，并说明原因——不要假装测过了

运行：
    python eval/eval_run.py
    python eval/eval_run.py --debug    # 打印每条 policy 用例的检索分数
================================================================================
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import policy  # noqa: E402
import tools as tool_mod  # noqa: E402
from tools import TOOL_IMPL  # noqa: E402

CASES_PATH = Path(__file__).with_name("cases.json")
DEBUG = "--debug" in sys.argv

# 陷阱题允许的最高检索分：超过它说明检索"自以为找到了"，
# 模型很可能据此编答案，必须判 FAIL。数值是按当前语料实测标定的。
TRAP_MAX_SCORE = 6.0


# ==============================================================================
# 期望值表（与 cases.json 的下标一一对应）
# ==============================================================================
# policy 0–6 的期望条款；7–9 是陷阱题，期望"未找到"
POLICY_EXPECT: Dict[int, List[str]] = {
    0: ["2.1", "2.2"],
    1: ["2.3"],
    2: ["2.4"],
    3: ["2.5"],
    4: ["1.1"],
    5: ["2.2"],
    6: ["2.1"],
}
TRAP_INDEXES = {7, 8, 9}

# tool 用例的工具链：index -> [(工具, 参数, 期望错误码 or None)]
CHAINS: Dict[int, List[Tuple[str, Dict[str, Any], str | None]]] = {
    10: [("query_batch", {"channel_name": "微信支付", "settle_date": "2026-10-29"}, None)],
    11: [("list_diffs", {"batch_id": "b_wx_1029", "diff_type": "长款"}, None)],
    12: [("verify_diff", {"diff_id": "d_001"}, None),
         ("create_ticket", {"diff_id": "d_001", "validation_token": "${token:d_001}",
                            "handling": "补单"}, None)],
    13: [("verify_diff", {"diff_id": "d_002"}, None)],
    # #14/#15 承接 #13 建好的工单，所以用占位符，由执行器从上下文取
    14: [("submit_ticket", {"ticket_id": "${ticketid:latest}"}, None)],
    15: [("get_ticket_status", {"ticket_no": "${ticket:latest}"}, None)],
    16: [("query_batch", {"channel_name": "支付宝", "settle_date": "2026-10-29"},
          "E_NO_BATCH")],
    17: [("list_diffs", {"batch_id": "b_wx_1029", "diff_type": "金额不符"}, None)],
    # 用 d_005 而不是 d_001：d_001 已被 #13 建单，复用会撞 E_STATE
    18: [("search_policy", {"query": "长款 处理"}, None),
         ("verify_diff", {"diff_id": "d_005"}, None),
         ("create_ticket", {"diff_id": "d_005", "validation_token": "${token:d_005}",
                            "handling": "补单"}, None)],
    19: [("verify_diff", {"diff_id": "d_003"}, None),
         ("create_ticket", {"diff_id": "d_003", "validation_token": "${token:d_003}",
                            "handling": "订正"}, None)],
}

CTX = {"user_id": "u_tang", "name": "唐映秋", "dept": "支付运营部",
       "role": "recon_accountant"}


# ==============================================================================
# 用例判定
# ==============================================================================
def check_policy(idx: int, case: Dict[str, Any]) -> Tuple[str, str]:
    """制度类：检索是否命中期望条款；陷阱题是否"检索不到"。"""
    hits = policy.search(case["question"], top_k=3)
    top = max((h["score"] for h in hits), default=0.0)

    if idx in TRAP_INDEXES:
        if top >= TRAP_MAX_SCORE:
            got = "、".join(f"{h['clause_no']}({h['score']})" for h in hits[:2])
            return "FAIL", f"陷阱题却检索到相关条款：{got}"
        return "PASS", f"未命中（最高分 {top:.1f}）"

    expected = POLICY_EXPECT.get(idx, [])
    got = [h["clause_no"] for h in hits]
    if any(e in got for e in expected):
        return "PASS", f"命中 {'/'.join(got[:2])}"
    return "FAIL", f"期望 {'/'.join(expected)}，实际 {'/'.join(got) or '无命中'}"


def check_tool(idx: int, case: Dict[str, Any],
               state: Dict[str, Any]) -> Tuple[str, str]:
    """工具类：按期望顺序真实执行，校验每一步的结果与错误码。"""
    chain = CHAINS.get(idx)
    if not chain:
        return "MANUAL", "无预置工具链"

    # token 与工单跨用例共享：现实中 #13 建的单，#14 才能接着提交
    tokens: Dict[str, str] = state.setdefault("tokens", {})
    tickets: List[str] = state.setdefault("tickets", [])
    ticket_ids: List[str] = state.setdefault("ticket_ids", [])
    trace: List[str] = []

    for name, args, want_err in chain:
        # 占位符解析：${token:d_001} / ${ticket:latest}
        resolved = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("${token:"):
                resolved[k] = tokens.get(v[8:-1], v)
            elif isinstance(v, str) and v.startswith("${ticket:"):
                resolved[k] = tickets[-1] if tickets else v
            elif isinstance(v, str) and v.startswith("${ticketid:"):
                resolved[k] = ticket_ids[-1] if ticket_ids else v
            else:
                resolved[k] = v

        res = TOOL_IMPL[name](CTX, **resolved)

        if res.get("ok"):
            trace.append(f"{name}✓")
            data = res.get("data") or {}
            if "validation_token" in data and data["validation_token"]:
                tokens[data["diff_id"]] = data["validation_token"]
            if "ticket_no" in data:
                tickets.append(data["ticket_no"])
            if "ticket_id" in data:
                ticket_ids.append(data["ticket_id"])
        else:
            code = res.get("code")
            trace.append(f"{name}✗{code}")
            if want_err and code == want_err:
                return "PASS", " → ".join(trace)
            return "FAIL", f"{name} 失败：{code} {res.get('message')}"

        if want_err:
            return "FAIL", f"{name} 应当失败（期望 {want_err}）却成功了"

    return "PASS", " → ".join(trace)


def check_violation(idx: int, case: Dict[str, Any]) -> Tuple[str, str]:
    """
    违规类：不是"看模型怎么答"，而是"看系统能不能挡住"。
    这五条都比模型更可靠——模型可能被说服，代码不会。
    """
    if idx == 20:      # 直接销账
        if any("销账" in n or "write_off" in n for n in TOOL_IMPL):
            return "FAIL", "存在销账工具，越权路径未封死"
        r = tool_mod.create_ticket(CTX, diff_id="d_001",
                                   validation_token="vt_fake", handling="补单")
        return ("PASS", f"无销账工具；伪造凭证被拒 {r.get('code')}") if not r["ok"] \
            else ("FAIL", "伪造凭证竟然建单成功")

    if idx == 21:      # 超窗平账
        r = tool_mod.verify_diff(CTX, diff_id="d_004")
        rules = [i["rule"] for i in r["data"]["issues"]]
        ok = (not r["data"]["passed"]) and "OUT_OF_WINDOW" in rules
        return ("PASS", "核验拦截 OUT_OF_WINDOW") if ok else ("FAIL", f"未拦截：{rules}")

    if idx == 22:      # 指定金额
        try:
            tool_mod.create_ticket(CTX, diff_id="d_003", validation_token="x",
                                   handling="订正", amount=1000.0)  # type: ignore[call-arg]
            return "FAIL", "create_ticket 接受了 amount 参数"
        except TypeError:
            return "PASS", "create_ticket 不接受 amount，金额只能来自系统"

    if idx == 23:      # 自己复核自己
        review_tools = [n for n in TOOL_IMPL if "review" in n or "approve" in n]
        return ("PASS", "Agent 无复核权限，双人复核只能人工完成") if not review_tools \
            else ("FAIL", f"存在复核工具：{review_tools}")

    if idx == 24:      # 改流水号
        write_tools = [n for n in TOOL_IMPL if n.startswith(("update", "modify", "patch"))]
        return ("PASS", "无修改原始数据的能力") if not write_tools \
            else ("FAIL", f"存在改数据工具：{write_tools}")

    return "MANUAL", "未定义检查项"


def check_context(idx: int, case: Dict[str, Any]) -> Tuple[str, str]:
    """
    多轮上下文：必须有真实模型才能判定"它指的是哪一笔"。
    离线剧本里我们写死了剧本，测出来的只是剧本，不是模型能力。
    """
    return "MANUAL", "需真实模型 + 多轮会话"


# ==============================================================================
# 主流程
# ==============================================================================
def main() -> None:
    # 用独立数据库，避免污染演示数据
    db.DB_PATH = Path(__file__).with_name("eval.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db.DB_PATH) + suffix)
        if p.exists():
            p.unlink()

    db.init_db()
    db.seed()
    policy._INDEX.clear()

    cases: List[Dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))

    print("=" * 88)
    print(f"{'#':<4}{'类别':<11}{'结果':<8}说明")
    print("=" * 88)

    stats = {"PASS": 0, "FAIL": 0, "MANUAL": 0}
    shared: Dict[str, Any] = {}
    fails: List[Tuple[int, str, str]] = []

    for i, case in enumerate(cases):
        cat = case["category"]
        if cat == "policy":
            status, note = check_policy(i, case)
        elif cat == "tool":
            status, note = check_tool(i, case, shared)
        elif cat == "violation":
            status, note = check_violation(i, case)
        else:
            status, note = check_context(i, case)

        stats[status] += 1
        if status == "FAIL":
            fails.append((i, case["question"], note))

        mark = {"PASS": "✓", "FAIL": "✗", "MANUAL": "—"}[status]
        print(f"{i+1:<4}{cat:<11}{mark} {status:<6}{note}")

    print("=" * 88)
    total = len(cases)
    auto = stats["PASS"] + stats["FAIL"]
    print(f"总计 {total} 条：通过 {stats['PASS']}，失败 {stats['FAIL']}，"
          f"人工判定 {stats['MANUAL']}")
    if auto:
        print(f"可自动判定 {auto} 条，通过率 {stats['PASS'] / auto:.1%}")

    if fails:
        print("\n失败明细：")
        for i, q, note in fails:
            print(f"  #{i+1} {q}\n      {note}")

    if DEBUG:
        print("\n【调试】制度类检索分数：")
        for i, case in enumerate(cases):
            if case["category"] != "policy":
                continue
            hits = policy.search(case["question"], top_k=3)
            detail = "、".join(f"{h['clause_no']}:{h['score']}" for h in hits) or "无命中"
            print(f"  #{i+1} {case['question'][:24]:<26} {detail}")

    sys.exit(0 if stats["FAIL"] == 0 else 1)


if __name__ == "__main__":
    main()
