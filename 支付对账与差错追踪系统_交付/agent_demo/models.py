#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/models.py —— 决策模型适配层
================================================================================
两种实现，接口一致，循环层不关心用谁：

  ScriptedModel  离线演示：按预置剧本返回决策，不联网、不花钱。
                 作用是"让循环跑起来看轨迹"，不是"假装模型很聪明"。
  OpenAIModel    真实调用：把工具 schema 交给模型，由它决定调哪个。
                 配了 LLM_API_KEY 就自动启用。

统一接口：
    decide(messages, tools) -> {"tool_calls": [...], "content": "...", "final": bool}
================================================================================
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List


class BaseModel:
    def decide(self, messages: List[Dict[str, Any]],
               tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError


# ==============================================================================
# 剧本模型（离线测试替身）
# ==============================================================================
class ScriptedModel(BaseModel):
    """
    按剧本依次返回决策。

    元素两类：
      {"calls": [{"name": ..., "args": {...}}]}   → 调工具
      {"final": "回答文本"}                        → 结束
    """

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.pos = 0

    def decide(self, messages, tools) -> Dict[str, Any]:
        if self.pos >= len(self.script):
            return {"tool_calls": [], "content": "（剧本已结束）", "final": True}

        step = self.script[self.pos]
        self.pos += 1

        if "final" in step:
            return {"tool_calls": [], "content": step["final"], "final": True}

        return {"tool_calls": step.get("calls", []),
                "content": step.get("content", ""),
                "final": False}


# ==============================================================================
# OpenAI 兼容模型（真实）
# ==============================================================================
class OpenAIModel(BaseModel):
    """走 OpenAI 兼容协议：DeepSeek / 通义 / vLLM / One-API 网关都能用。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def decide(self, messages, tools) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"模型接口 {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"模型服务不可达：{e.reason}") from e

        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"name": fn.get("name", ""), "args": args})

        return {"tool_calls": calls,
                "content": msg.get("content") or "",
                "final": not calls and bool(msg.get("content"))}


def make_model(script: List[Dict[str, Any]] | None = None) -> BaseModel:
    """工厂：有 Key 用真实模型，没有就退到剧本模型，保证任何机器都能跑。"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key and script is None:
        return OpenAIModel(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=key,
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        )
    return ScriptedModel(script or [])
