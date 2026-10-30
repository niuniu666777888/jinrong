#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/demo_run.py —— 跑典型对话，打印完整轨迹
================================================================================
这既是演示脚本，也是验收用例：每个场景的预期轨迹写在剧本注释里，
跑出来的事件序列必须和它一致，不一致就说明循环、确认门或守卫有问题。

场景：
  a. 只查规范（不应调用任何写工具）
  b. 完整处理（查规范→查批次→列差异→核验→建单→确认）
  c. 单条差异能不能销账
  d. 越权测试（模型伪造核验凭证，应在服务端被拒）

运行：
    python demo_run.py
    python demo_run.py --keep-db     # 保留数据库，跑完可自己查 agent_log
================================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

import db
import tools as tool_mod
from loop import AgentRunner
from models import ScriptedModel

# ==============================================================================
# 剧本
# ==============================================================================
# ${token:d_001} 是占位符，执行前由 AgentRunner 用 verify_diff 返回的真实 token 替换。
# 真实模型不需要占位符——它自己从工具结果里读。

SCENARIO_A = [
    {"calls": [{"name": "search_policy",
                "args": {"query": "长款 短款 区别 处理"}}]},
    {"final": ("两者方向相反，处理方式也不同：\n"
               "· 长款：渠道有交易记录、内部无有效订单（平台多收），处理为补单或原路退款。\n"
               "· 短款：内部显示支付成功、渠道无对应流水（平台少收），处理为向渠道调单核查，"
               "必要时补偿扣款或冻结订单。\n"
               "依据《差错处理规范》第 2.1、2.2 条（2026 版）。\n"
               "两条都要求在 7 天回溯窗口内处理完毕（2.3 条）。")},
]

SCENARIO_B = [
    # 第 1 轮：查规范 + 查批次（互不依赖，同一轮并发）
    {"calls": [
        {"name": "search_policy", "args": {"query": "长款 处理 补单 退款"}},
        {"name": "query_batch", "args": {"channel_name": "微信支付",
                                         "settle_date": "2026-10-29"}},
    ]},
    # 第 2 轮：列出长款差异
    {"calls": [{"name": "list_diffs",
                "args": {"batch_id": "b_wx_1029", "diff_type": "长款", "limit": 5}}]},
    # 第 3 轮：核验其中一条
    {"calls": [{"name": "verify_diff", "args": {"diff_id": "d_001"}}]},
    # 第 4 轮：建工单（会被确认门拦下）
    {"calls": [{"name": "create_ticket",
                "args": {"diff_id": "d_001",
                         "validation_token": "${token:d_001}",
                         "handling": "补单",
                         "note": "渠道有流水、内部无订单，补单处理"}}]},
    # 第 5 轮：收尾
    {"final": ("已为差异 d_001 建单：长款 1,280.00 元，处理方式为补单。\n"
               "该差异在 7 天回溯窗口内，符合《差错处理规范》2.1 条。\n"
               "工单需由另一名经办人复核后方可销账（2.4 条双人复核）。")},
]

SCENARIO_C = [
    {"calls": [{"name": "verify_diff", "args": {"diff_id": "d_002"}}]},
    {"final": ("这笔短款差异本身成立（内部支付成功、渠道无流水），可以建工单处理，"
               "但不能“直接销账”：\n"
               "· 销账须双人复核，经办人提交、另一人签核（《差错处理规范》2.4 条）；\n"
               "· 处理方式应选“调单”或“补偿扣款”（2.2 条）。\n"
               "需要我现在为它建工单吗？")},
]

# 越权：模型伪造凭证直接建单，模拟"不听话的模型"
SCENARIO_D = [
    {"calls": [{"name": "create_ticket",
                "args": {"diff_id": "d_003",
                         "validation_token": "vt_我编的凭证",
                         "handling": "订正"}}]},
    {"final": ("建单被系统拒绝：核验凭证无效。\n"
               "这说明“跳过核验直接建单”在服务端走不通——拦它的是签名校验，不是提示词。")},
]


# ==============================================================================
# 打印
# ==============================================================================
def print_events(events) -> None:
    for ev in events:
        kind = ev["type"]

        if kind == "round":
            print(f"\n[第 {ev['round']} 轮]")

        elif kind == "assistant":
            print(f"  模型：{ev['text'][:120]}")

        elif kind == "tool_call":
            brief = ", ".join(f"{k}={v}" for k, v in list(ev["args"].items())[:2])
            print(f"  → 调用 {ev['name']}({brief}{'…' if len(ev['args']) > 2 else ''})")

        elif kind == "tool_result":
            mark = "✓" if ev["ok"] else "✗"
            print(f"  ← {mark} {ev['name']}  {ev['summary']}  ({ev['elapsed_ms']} ms)")

        elif kind == "stream":
            print(f"      | {ev['text']}")

        elif kind == "confirm_request":
            print("  ⏸  等待员工确认…")

        elif kind == "confirm_result":
            print(f"  {'✅ 已确认' if ev['approved'] else '⛔ 已取消'}")

        elif kind == "final":
            print("\n  最终回答：\n    " + ev["text"].replace("\n", "\n    "))

        elif kind == "limit":
            print(f"\n  ⚠ {ev['message']}")

        elif kind == "error":
            print(f"\n  ⚠ {ev['message']}")


def run_scenario(title: str, user_input: str, script, approve: bool = True) -> None:
    print("\n" + "=" * 74)
    print(f"场景 {title}")
    print("=" * 74)
    print(f"用户 > {user_input}")

    runner = AgentRunner(
        model=ScriptedModel(script),
        ctx={"user_id": "u_tang", "name": "唐映秋", "dept": "支付运营部",
             "role": "recon_accountant"},
        session_id=f"s_{abs(hash(title)) % 10000:04d}",
        confirm_handler=lambda preview: approve,
    )
    print_events(runner.run(user_input))


# ==============================================================================
def main() -> None:
    keep = "--keep-db" in sys.argv
    db_file = Path(__file__).with_name("agent_demo.db")
    if db_file.exists() and not keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_file) + suffix)
            if p.exists():
                p.unlink()

    db.init_db()
    db.seed()

    run_scenario("a. 只查规范（不应触发任何写操作）",
                 "长款和短款有什么区别？", SCENARIO_A)

    run_scenario("b. 完整处理一条长款差异（查规范→查批次→列差异→核验→建单→确认）",
                 "帮我处理昨天微信渠道的长款差异", SCENARIO_B, approve=True)

    run_scenario("c. 单条差异能否销账",
                 "这笔短款差异能直接销账吗？", SCENARIO_C)

    run_scenario("d. 越权测试（伪造核验凭证）",
                 "直接建单，不用核验了", SCENARIO_D, approve=True)

    # 额外验证：超窗差异应被业务规则挡住（不建单，只核验）
    print("\n" + "=" * 74)
    print("补充验证：超过 7 天回溯窗口的差异")
    print("=" * 74)
    res = tool_mod.verify_diff(
        {"user_id": "u_tang", "name": "唐映秋", "dept": "支付运营部",
         "role": "recon_accountant"},
        diff_id="d_004",
    )
    print(f"  verify_diff(d_004) → passed={res['data']['passed']}")
    for i in res["data"]["issues"]:
        print(f"    [{i['level']}] {i['code'] if 'code' in i else i['rule']}：{i['message']}")

    # ---------------- 日志表 ----------------
    print("\n" + "=" * 74)
    print("agent_log（阶段 5 分析的数据来源）")
    print("=" * 74)
    rows = db.q("SELECT session_id, round_no, tool_name, ok, blocked, summary, elapsed_ms "
                "FROM agent_log ORDER BY created_at")
    print(f"{'会话':<8}{'轮':<4}{'工具':<18}{'结果':<6}{'耗时':<8}摘要")
    for r in rows:
        flag = "拦截" if r["blocked"] else ("成功" if r["ok"] else "失败")
        print(f"{r['session_id']:<8}{r['round_no']:<4}{r['tool_name']:<18}"
              f"{flag:<6}{r['elapsed_ms']:<8}{r['summary'][:44]}")

    # ---------------- 结果核对 ----------------
    tickets = db.q("SELECT ticket_no, diff_type, handling, amount, status FROM ticket")
    print("\n工单表：")
    for t in tickets:
        print(f"  {t['ticket_no']}  {t['diff_type']}  {t['handling']}  "
              f"{t['amount']:.2f} 元  {t['status']}")

    print("\n预期：场景 b 生成 1 张工单；场景 d 不产生任何工单。")
    print("=" * 74)


if __name__ == "__main__":
    main()
