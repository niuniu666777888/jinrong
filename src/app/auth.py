"""登录与会话：PBKDF2 口令校验 + HMAC 签名 Cookie。

MVP 取舍：
- 不做服务端会话表，登录态全在签名 Cookie 里（改不了、过期失效）；
- 退出登录只清 Cookie，不吊销令牌——单机演示够用，生产要加会话表与黑名单。
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

from . import config, db
from .errors import CODE_UNAUTHORIZED, BizError

COOKIE_NAME = "mvp_session"
TOKEN_TTL = int(os.environ.get("MVP_SESSION_TTL", str(8 * 3600)))
PBKDF2_ROUNDS = 120_000


# ---------------------------------------------------------------- 口令
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, expected = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), expected)


# ---------------------------------------------------------------- 令牌
def _sign(raw: str) -> str:
    return hmac.new(config.SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


def issue(user_id: str) -> str:
    payload = json.dumps({"u": user_id, "exp": int(time.time()) + TOKEN_TTL},
                         separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def parse(token: str) -> Optional[str]:
    """校验签名与过期，返回 user_id。"""
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    if not hmac.compare_digest(mac, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (json.JSONDecodeError, ValueError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("u")


# ---------------------------------------------------------------- 用户
def user_by_no(employee_no: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM app_user WHERE employee_no = ?", (employee_no,))


def user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM app_user WHERE id = ?", (user_id,))


def login(employee_no: str, password: str) -> Dict[str, Any]:
    user = user_by_no((employee_no or "").strip())
    if not user or not verify_password(password or "", user["password_hash"]):
        # 不区分「用户不存在」与「密码错误」，避免撞库探测
        raise BizError(CODE_UNAUTHORIZED, "账号或密码不正确", 401)
    return user


def current_user(request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(COOKIE_NAME)
    user_id = parse(token) if token else None
    return user_by_id(user_id) if user_id else None


def require_login(request) -> Dict[str, Any]:
    user = current_user(request)
    if not user:
        raise BizError(CODE_UNAUTHORIZED, "请先登录", 401)
    return user


def require_role(user: Dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise BizError(CODE_UNAUTHORIZED, "当前账号没有该操作权限", 403)
