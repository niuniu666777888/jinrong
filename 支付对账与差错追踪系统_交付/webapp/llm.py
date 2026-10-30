#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/llm.py —— 流式大模型调用
================================================================================
在阶段 1 的 LLMClient 基础上，只加一件事：把「一次性拿完整回答」变成
「逐段吐 token」。其余（供应商切换、mock 兜底、错误处理）全部继承，不重写。

为什么要单独一个文件：
  非流式调用用 _post_json 一次读完；流式调用要按行读 socket，
  两者对超时、异常、编码的处理完全不同，混在一起会很难维护。
================================================================================
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Dict, Iterator, List, Optional

import qa_prototype
from qa_prototype import Config, LLMClient


class StreamingLLM(LLMClient):
    """
    【核心】带流式输出的 LLM 客户端。

    对外只多一个方法：chat_stream(messages) -> Iterator[str]
    调用方拿到的是"一段一段的文字"，直接转发给前端即可，
    不需要自己判断是哪家供应商。
    """

    # ------------------------------------------------------------------
    def chat_stream(self, messages: List[Dict[str, str]],
                    temperature: Optional[float] = None) -> Iterator[str]:
        """
        流式生成。逐段 yield 文本增量（delta），结束时自然停止。

        注意：这里不拼接完整回答，由调用方负责累积——
        因为调用方还要在中断时保存"已经生成的部分"，它必须自己持有缓冲。
        """
        temp = self.cfg.llm_temperature if temperature is None else temperature

        if self.provider == "mock":
            yield from self._stream_mock(messages)
            return

        if self.provider in ("openai", "deepseek"):
            yield from self._stream_openai(messages, temp)
            return

        if self.provider == "ollama":
            yield from self._stream_ollama(messages, temp)
            return

        raise ValueError(f"未知的 LLM_PROVIDER：{self.provider}")

    # ------------------------------------------------------------------
    def _stream_openai(self, messages: List[Dict[str, str]], temperature: float) -> Iterator[str]:
        """
        OpenAI 兼容协议的流式接口。

        协议要点：
          - 响应是 text/event-stream，每一帧形如 `data: {...}`
          - 结束标记是 `data: [DONE]`
          - 增量在 choices[0].delta.content，有些厂商首次返回 role 而不是内容，
            所以要判空，不能假设每个 delta 都有 content
        """
        url = f"{self.cfg.llm_base_url}/chat/completions"
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,          # 关键：开启流式
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.llm_api_key}"

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout) as resp:
                # 逐行读：http.client 的响应对象支持按行迭代，不用自己切 buffer
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        # 单帧解析失败不致命，跳过即可——流式里偶尔会混进心跳包
                        continue

                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"模型接口返回 {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"模型服务连接失败：{e.reason}") from e

    # ------------------------------------------------------------------
    def _stream_ollama(self, messages: List[Dict[str, str]], temperature: float) -> Iterator[str]:
        """本地 Ollama 的流式接口：返回的是一行一个 JSON，而不是 SSE。"""
        base = self.cfg.llm_base_url or "http://127.0.0.1:11434"
        payload = {
            "model": self.cfg.llm_model or "qwen2.5:7b",
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/chat", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = (obj.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if obj.get("done"):
                        break
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama 连接失败：{e.reason}") from e

    # ------------------------------------------------------------------
    def _stream_mock(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        """
        mock 流式：把抽取式答案切成小段慢慢吐。

        加 20ms 间隔不是"假装很慢"，而是为了让前端的打字机效果可见——
        否则几百字瞬间刷完，等于没有流式，联调时看不出问题。
        """
        text = self._chat_mock(messages)

        step = 8            # 每段 8 个字，观感接近真实 token 流
        for i in range(0, len(text), step):
            yield text[i:i + step]
            time.sleep(0.02)
