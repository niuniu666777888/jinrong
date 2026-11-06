"""统一错误模型。

业务码与 HTTP 状态码一一对应，前端只认 code。所有面向用户的消息都是中文，
内部异常原文一律不外传（只进日志）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

CODE_OK = 0
CODE_BAD_REQUEST = 10400
CODE_UNAUTHORIZED = 10402
CODE_EMPTY_QUESTION = 10401
CODE_NOT_FOUND = 10404
CODE_STATE = 10409
CODE_FORBIDDEN = 10403
CODE_TOO_FREQUENT = 10429
CODE_RETRIEVE_FAIL = 10500
CODE_MODEL_FAIL = 10501
CODE_NOT_READY = 10502


@dataclass
class BizError(Exception):
    code: int
    message: str
    http_status: int = 400
    extra: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:      # pragma: no cover - 便于本地排查
        return f"[{self.code}] {self.message}"


def body(code: int, message: str, data: Optional[Any] = None,
         trace_id: str = "") -> Dict[str, Any]:
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def ok(data: Any = None, trace_id: str = "") -> Dict[str, Any]:
    return body(CODE_OK, "ok", data, trace_id)
