#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
client/api.py —— 客户端传输层（不含界面，可单独测试）
================================================================================
为什么把网络层和界面拆开：
  1. 界面代码没法做自动化测试，网络层可以；
  2. 以后换成 Qt / Electron / 小程序，这一层原样带走。

只依赖标准库：urllib + http.cookiejar。登录后 Cookie 自动带着，
和浏览器访问是同一套会话机制，不需要自己管 token。
================================================================================
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional


class ApiError(Exception):
    """服务端返回的业务错误（带 code 与中文 message）。"""

    def __init__(self, code: int, message: str, http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class ChatClient:
    """对服务端接口的封装。所有方法失败时抛 ApiError。"""

    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # CookieJar 负责保存登录态；HTTPCookieProcessor 负责自动回填
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None,
                 form: Optional[Dict[str, str]] = None) -> Any:
        url = self.base_url + path
        data = None
        headers = {}

        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip().startswith("{") else body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
                raise ApiError(obj.get("code", e.code), obj.get("message", str(e)), e.code)
            except json.JSONDecodeError:
                raise ApiError(e.code, f"服务端返回异常（HTTP {e.code}）", e.code)
        except urllib.error.URLError as e:
            raise ApiError(-1, f"无法连接服务端：{e.reason}")

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def login(self, employee_no: str, password: str) -> Dict[str, Any]:
        """
        表单登录。成功后服务端下发 HttpOnly Cookie，CookieJar 自动保存。

        注意：/login 成功会 302 到首页，urllib 会自动跟随，
        所以我们再调一次 /api/v1/auth/me 确认登录态真的生效——
        只看 302 是不够的，密码错时服务端也返回 200 的登录页。
        """
        self._request("POST", "/login",
                      form={"employee_no": employee_no, "password": password,
                            "next": "/"})
        me = self.me()
        if not me:
            raise ApiError(40101, "工号或密码不正确")
        return me

    def me(self) -> Optional[Dict[str, Any]]:
        """取当前登录用户；未登录返回 None（不抛异常，便于界面判断）。"""
        try:
            data = self._request("GET", "/api/v1/auth/me")
        except ApiError as e:
            if e.code == 40101:
                return None
            raise
        return (data or {}).get("data")

    def logout(self) -> None:
        try:
            self._request("POST", "/api/v1/auth/logout")
        except ApiError:
            pass

    # ------------------------------------------------------------------
    # 聊天（流式）
    # ------------------------------------------------------------------
    def chat(self, session_id: str, question: str) -> Iterator[Dict[str, Any]]:
        """
        发起提问并按事件逐条产出：{"type": "delta"/"sources"/"status"/..., ...}

        与浏览器端保持同一套 SSE 协议，所以客户端不需要理解业务，
        只负责把事件渲染出来。
        """
        url = self.base_url + "/api/v1/chat"
        body = json.dumps({"session_id": session_id, "question": question},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )

        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
                raise ApiError(obj.get("code", e.code), obj.get("message", str(e)), e.code)
            except json.JSONDecodeError:
                raise ApiError(e.code, f"服务端返回异常（HTTP {e.code}）", e.code)
        except urllib.error.URLError as e:
            raise ApiError(-1, f"无法连接服务端：{e.reason}")

        buffer = ""
        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                buffer += line

                # 按空行切帧；TCP 分包会把一帧拆开，必须自己维护 buffer
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    evt = self._parse_frame(frame)
                    if evt:
                        yield evt

    @staticmethod
    def _parse_frame(frame: str) -> Optional[Dict[str, Any]]:
        event, data_lines = "message", []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return None
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return None
        return {"type": event, **data}

    # ------------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------------
    def fetch_source(self, chunk_id: str) -> Dict[str, Any]:
        """取引用条文原文，用于点击来源时弹详情。"""
        data = self._request("GET", f"/api/v1/sources/{chunk_id}")
        return data.get("data") or {}

    def history(self, session_id: str) -> List[Dict[str, Any]]:
        """拉历史消息，用于恢复上次对话。"""
        data = self._request("GET", f"/api/v1/sessions/{session_id}/messages")
        return (data.get("data") or {}).get("messages") or []

    # ------------------------------------------------------------------
    # 管理端：知识库
    # ------------------------------------------------------------------
    def list_docs(self, keyword: str = "", page: int = 1, size: int = 50) -> Dict[str, Any]:
        qs = urllib.parse.urlencode({"keyword": keyword, "page": page, "size": size})
        return (self._request("GET", f"/api/v1/admin/kb/docs?{qs}") or {}).get("data") or {}

    def upload_doc(self, file_path: str) -> Dict[str, Any]:
        """
        上传文档。

        用 urllib 手工拼 multipart：标准库没有现成的 multipart 编码器，
        但格式很简单——一个 boundary + 若干段 headers + 内容。
        注意必须用二进制拼接，文件可能是 PDF。
        """
        path = __import__("pathlib").Path(file_path)
        boundary = "----dsh" + __import__("uuid").uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + path.read_bytes() + tail

        req = urllib.request.Request(
            self.base_url + "/api/v1/admin/kb/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
                raise ApiError(obj.get("code", e.code), obj.get("message", str(e)), e.code)
            except json.JSONDecodeError:
                raise ApiError(e.code, f"上传失败（HTTP {e.code}）", e.code)

        if data.get("code") != 0:
            raise ApiError(data.get("code", -1), data.get("message", "上传失败"))
        return data.get("data") or {}

    def delete_doc(self, doc_id: str) -> Dict[str, Any]:
        data = self._request("DELETE", f"/api/v1/admin/kb/docs/{doc_id}")
        return data or {}

    def reindex(self) -> Dict[str, Any]:
        return (self._request("POST", "/api/v1/admin/kb/reindex") or {}).get("data") or {}

    # ------------------------------------------------------------------
    # 管理端：提示词
    # ------------------------------------------------------------------
    def get_prompt(self) -> Dict[str, Any]:
        return (self._request("GET", "/api/v1/admin/prompts") or {}).get("data") or {}

    def save_prompt(self, template_id: str, content: str) -> Dict[str, Any]:
        return self._request("PUT", f"/api/v1/admin/prompts/{template_id}",
                             payload={"content": content}) or {}

    # ------------------------------------------------------------------
    # 管理端：审计
    # ------------------------------------------------------------------
    def audit_list(self, employee_no: str = "", date_from: str = "", date_to: str = "",
                   keyword: str = "", flagged: str = "", page: int = 1,
                   size: int = 50) -> Dict[str, Any]:
        qs = urllib.parse.urlencode({
            "employee_no": employee_no, "date_from": date_from, "date_to": date_to,
            "keyword": keyword, "flagged": flagged, "page": page, "size": size,
        })
        return (self._request("GET", f"/api/v1/admin/audit/messages?{qs}") or {}).get("data") or {}

    def audit_detail(self, message_id: str) -> Dict[str, Any]:
        return (self._request("GET", f"/api/v1/admin/audit/messages/{message_id}") or {}).get("data") or {}

    def flag(self, message_id: str, category: str, reason: str = "") -> Dict[str, Any]:
        return self._request("POST", f"/api/v1/admin/audit/messages/{message_id}/flag",
                             payload={"category": category, "reason": reason}) or {}

    def unflag(self, message_id: str) -> Dict[str, Any]:
        return self._request("DELETE",
                             f"/api/v1/admin/audit/messages/{message_id}/flag") or {}
