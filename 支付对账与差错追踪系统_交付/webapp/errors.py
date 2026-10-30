#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/errors.py —— 统一错误模型与错误码
================================================================================
从 app.py 里抽出来独立成模块，是因为 admin.py / auth.py 也要抛同样的异常；
如果各自定义一份，app.py 里注册的处理器就接不住别的模块抛出来的异常
（Python 按类对象判断，同名不同类不算同一个）。
================================================================================
"""
from __future__ import annotations

from typing import Any, Dict


class BizError(Exception):
    """业务异常：带业务码、HTTP 状态码和中文提示，前端可直接展示。"""

    def __init__(self, code: int, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ==============================================================================
# 错误码
# ==============================================================================
CODE_OK = 0

# --- 参数与资源 ---
CODE_BAD_PARAM = 10400          # 参数格式错误
CODE_EMPTY_QUESTION = 10401     # 问题为空或超长
CODE_NO_SESSION = 10404         # 会话 / 资源不存在
CODE_TOO_MANY = 10406           # 单会话消息达上限
CODE_RATE_LIMIT = 10429         # 请求过于频繁（限流）
CODE_BAD_FILE_TYPE = 10409      # 不支持的文件类型
CODE_FILE_TOO_LARGE = 10410     # 文件超过大小上限
CODE_PARSE_FAILED = 10411       # 文件解析失败
CODE_DUPLICATE_DOC = 10412      # 文档重复
CODE_EMPTY_PROMPT = 10413       # 提示词为空或超长
CODE_LAST_PROMPT = 10415        # 不能删除唯一启用的模板

# --- 服务 ---
CODE_RETRIEVE_FAIL = 10500      # 检索失败
CODE_MODEL_FAIL = 10501         # 模型调用失败
CODE_NOT_READY = 10502          # 知识库未就绪

# --- 鉴权 ---
CODE_UNAUTHORIZED = 40101       # 未登录 / 令牌过期
CODE_FORBIDDEN = 40103          # 无权限


def error_body(code: int, message: str, trace_id: str = "",
               data: Any = None) -> Dict[str, Any]:
    """统一的错误响应体。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}
