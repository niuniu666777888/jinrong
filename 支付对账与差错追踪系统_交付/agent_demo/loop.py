#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/loop.py —— Agent 执行循环（支付对账版）
================================================================================
循环：模型决策 → 执行工具 → 结果回填 → 再决策，最多 5 轮。

三个机制：

  ① 轮次上限 5
     兜住"模型死循环"：工具报参数错 → 模型改一个字段重试 → 还错 → 再改。
     没有上限会一直烧 token，而且用户什么也看不到。

  ② 确认门（create_ticket / submit_ticket）
     这两个工具一执行就产生业务数据，循环在这里停住：
     先把工单内容流式推给用户，拿到明确同意才继续。
     确认在"执行之前"，不是执行后回滚——事后回滚会留脏数据。

  ③ 每轮落日志
     工具名、参数、耗时、结果摘要、是否被拦，全进 agent_log，
     阶段 5 的"哪个环节总失败、哪类差异最容易卡住"就靠它。
================================================================================
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import db
import tools as tool_mod
from models import BaseModel
from tools import GUARDED_TOOLS, TOOL_IMPL, TOOL_SPECS

SYSTEM_PROMPT = """你是支付对账与差错追踪系统的业务助手，服务于对账会计与资金风险经理。
你不能调整资金、不能销账、不能修改原始流水与账单。

【最高优先级规则】
1. 任何关于"怎么处理、标准是什么"的问题，必须先调用 search_policy 检索规范，
   并复述条款号；检索不到就说"规范中未找到"，不得凭经验回答。
2. 建单必须严格按顺序：先查到具体差异（query_batch / list_diffs），
   再 verify_diff 核验，最后才允许 create_ticket。缺任何一步都不许建单。
3. verify_diff 返回 passed=false 时，你的任务结束于"解释哪条不达标"，
   不得尝试 create_ticket，也不得建议"先销一部分"。
4. 超过 7 天回溯窗口的差异，不得建议自动处理，必须转资金风险经理。
5. 金额、流水号、订单号一律引用工具返回值，不得自行推算或编造。

【工具使用】
- 每轮最多发起 2 个工具调用；互不依赖的可以同一轮并发。
- create_ticket / submit_ticket 执行前系统会要求人工确认，
  你应在确认之前先用自然语言说明"准备做什么、影响哪条差异"。
- 同一工具连续失败两次就停止，转人工并说明原因。

【回答格式】
先结论，再依据（带条款号），最后给下一步动作。金额写清币种与单位。
"""


class AgentRunner:
    """一次会话的执行器。同一实例可连续处理多条用户输入，保留上下文。"""

    def __init__(self, model: BaseModel, ctx: Dict[str, Any], session_id: str,
                 confirm_handler: Optional[Callable[[Dict[str, Any]], bool]] = None,
                 max_rounds: int = 5,
                 system_prompt: str = SYSTEM_PROMPT):
        self.model = model
        self.ctx = ctx
        self.session_id = session_id
        self.max_rounds = max_rounds
        self.system_prompt = system_prompt

        # 默认拒绝。宁可多问一句，也不能默认放行写操作。
        self.confirm_handler = confirm_handler or (lambda preview: False)

        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._round = 0

        # 跨轮记忆：差异 → 核验凭证，工单号 → 工单 ID。
        # 剧本模型用 ${token:d_001} 这样的占位符引用上一步结果；
        # 真实模型不需要占位符，它会自己从工具返回值里读。
        self._tokens: Dict[str, str] = {}
        self._tickets: Dict[str, str] = {}

    # ------------------------------------------------------------------
    def run(self, user_input: str) -> Iterator[Dict[str, Any]]:
        self.messages.append({"role": "user", "content": user_input})
        self._round = 0

        while self._round < self.max_rounds:
            self._round += 1
            yield {"type": "round", "round": self._round}

            # ---------- 1. 模型决策 ----------
            try:
                decision = self.model.decide(self.messages, TOOL_SPECS)
            except Exception as e:
                yield {"type": "error", "message": f"模型调用失败：{e}"}
                return

            text = (decision.get("content") or "").strip()
            calls = decision.get("tool_calls") or []

            if text:
                yield {"type": "assistant", "text": text}

            self.messages.append({
                "role": "assistant",
                "content": text,
                **({"tool_calls": [
                    {"id": f"call_{self._round}_{i}", "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                    for i, c in enumerate(calls)
                ]} if calls else {}),
            })

            if not calls:
                yield {"type": "final", "text": text or "（没有可执行的动作）"}
                return

            # ---------- 2. 执行工具 ----------
            for idx, call in enumerate(calls):
                name = call.get("name", "")
                args = self._resolve(call.get("args", {}) or {})
                call_id = f"call_{self._round}_{idx}"

                yield {"type": "tool_call", "name": name, "args": args,
                       "round": self._round}

                if name not in TOOL_IMPL:
                    result = {"ok": False, "code": "E_UNKNOWN_TOOL",
                              "message": f"不存在工具 {name}"}
                    yield {"type": "tool_result", "name": name, "ok": False,
                           "summary": result["message"], "elapsed_ms": 0}
                    self._append_tool_msg(call_id, name, result)
                    continue

                # ---------- 确认门 ----------
                if name in GUARDED_TOOLS:
                    preview = self._build_preview(name, args)
                    for line in self._preview_lines(preview):
                        yield {"type": "stream", "text": line}
                    yield {"type": "confirm_request", "tool": name, "preview": preview}

                    approved = bool(self.confirm_handler(preview))
                    yield {"type": "confirm_result", "approved": approved}

                    if not approved:
                        result = {"ok": False, "code": "E_USER_CANCELLED",
                                  "message": "用户取消了本次操作"}
                        db.log_call(self.session_id, self._round, name, args,
                                    ok=False, summary="用户取消", elapsed_ms=0,
                                    blocked=True)
                        yield {"type": "tool_result", "name": name, "ok": False,
                               "summary": "用户取消", "elapsed_ms": 0}
                        self._append_tool_msg(call_id, name, result)
                        continue

                # ---------- 执行 ----------
                started = time.time()
                try:
                    result = TOOL_IMPL[name](self.ctx, **args)
                except TypeError as e:
                    result = {"ok": False, "code": "E_BAD_ARGS", "message": f"参数错误：{e}"}
                except Exception as e:
                    result = {"ok": False, "code": "E_INTERNAL", "message": f"工具异常：{e}"}

                elapsed = int((time.time() - started) * 1000)
                summary = self._summarize(result)

                db.log_call(self.session_id, self._round, name, args,
                            ok=bool(result.get("ok")), summary=summary,
                            elapsed_ms=elapsed, blocked=False)

                yield {"type": "tool_result", "name": name, "ok": bool(result.get("ok")),
                       "summary": summary, "elapsed_ms": elapsed,
                       "code": result.get("code")}

                # 记住凭证/工单，供后续占位符解析
                if name == "verify_diff" and result.get("ok"):
                    data = result["data"]
                    if data.get("validation_token"):
                        self._tokens[data["diff_id"]] = data["validation_token"]
                if name == "create_ticket" and result.get("ok"):
                    data = result["data"]
                    self._tickets[data["ticket_no"]] = data["ticket_id"]

                self._append_tool_msg(call_id, name, result)

        yield {"type": "limit",
               "message": f"已达 {self.max_rounds} 轮上限，停止自动执行，请人工接手"}

    # ------------------------------------------------------------------
    def _append_tool_msg(self, call_id: str, name: str, result: Dict[str, Any]) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": json.dumps(result, ensure_ascii=False)[:2000],
        })

    # ------------------------------------------------------------------
    def _resolve(self, value: Any) -> Any:
        """
        解析占位符 ${token:d_001} / ${ticket:CT2026...}。

        这是给剧本模型用的便利：剧本是静态的，拿不到运行时的 token。
        真实模型会直接从工具返回值里取，不需要这个机制。
        """
        if isinstance(value, str):
            if value.startswith("${token:") and value.endswith("}"):
                return self._tokens.get(value[8:-1], value)
            if value.startswith("${ticket:") and value.endswith("}"):
                return self._tickets.get(value[9:-1], value)
            return value
        if isinstance(value, dict):
            return {k: self._resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        return value

    # ------------------------------------------------------------------
    @staticmethod
    def _summarize(result: Dict[str, Any]) -> str:
        """结果摘要：日志里存人话，不存整个 JSON（否则表很快爆炸）。"""
        if result.get("ok"):
            d = result.get("data") or {}
            if "ticket_no" in d:
                return f"工单 {d['ticket_no']} 已创建，{d.get('handling')}，{d.get('amount')} 元"
            if "passed" in d:
                n = len(d.get("issues") or [])
                return f"核验{'通过' if d['passed'] else f'未通过（{n} 项）'}"
            if "chunks" in d:
                return f"命中 {len(d['chunks'])} 条规范条款"
            if "diff_breakdown" in d:
                return f"批次差异 {d.get('diff_count')} 条：{d.get('diff_breakdown')}"
            if "items" in d:
                return f"列出 {d.get('count')} 条差异"
            return "成功"
        return f"{result.get('code', 'ERR')}：{result.get('message', '')}"

    # ------------------------------------------------------------------
    def _build_preview(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成给人看的预览。

        金额、类型一律重新查库，不采信模型传值——
        预览里看到的数字，必须和最终落库的完全一致。
        """
        if name == "submit_ticket":
            t = db.q1("SELECT * FROM ticket WHERE id = ?", (args.get("ticket_id", ""),))
            return {"kind": "submit",
                    "ticket_no": t["ticket_no"] if t else "（不存在）",
                    "amount": t["amount"] if t else 0,
                    "status": t["status"] if t else "-"}

        d = db.q1("SELECT * FROM diff_record WHERE id = ?", (args.get("diff_id", ""),))
        return {
            "kind": "ticket",
            "diff_id": args.get("diff_id"),
            "diff_type": d["diff_type"] if d else "（不存在）",
            "amount": d["amount"] if d else 0,
            "channel_txn_no": d["channel_txn_no"] if d else "",
            "handling": args.get("handling", ""),
            "risk": bool(d and d["amount"] > 3000),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _preview_lines(preview: Dict[str, Any]) -> List[str]:
        if preview["kind"] == "submit":
            return [
                f"即将提交工单 {preview['ticket_no']}（{preview['amount']:.2f} 元）复核，",
                "提交后由另一名经办人签核，不可撤回。",
            ]

        lines = ["即将创建差错工单："]
        lines.append(f"  · 差异类型：{preview['diff_type']}")
        lines.append(f"  · 渠道流水：{preview['channel_txn_no'] or '（无）'}")
        lines.append(f"  · 金额：{preview['amount']:.2f} 元")
        lines.append(f"  · 处理方式：{preview['handling']}")
        if preview.get("risk"):
            lines.append("  ⚠ 金额超过 3,000 元，将标注高风险并抄送资金风险经理")
        lines.append("确认创建吗？")
        return lines
