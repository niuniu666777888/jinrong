#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/auth.py —— 账号、密码、JWT 与权限校验
================================================================================
四件事：
  1. 密码哈希：用标准库 hashlib.pbkdf2_hmac，不引 bcrypt/passlib。
     格式 pbkdf2_sha256$迭代次数$salt_hex$hash_hex，自带参数，以后改迭代次数也能验旧密码。
  2. JWT：HS256，载荷带 role 和 ver（令牌版本号）。
  3. 取当前用户：从 HttpOnly Cookie 读，回查数据库确认账号仍有效、ver 仍匹配。
  4. 依赖注入：require_login / require_admin，路由里一行搞定。

为什么令牌放 Cookie 而不是 Authorization 头：
  后台是服务端渲染，页面跳转没法塞 header；放 Cookie 页面和接口都通。
  代价是要防 CSRF，用 SameSite=Lax + Origin 校验解决。
================================================================================
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
from fastapi import Request, Response

import admin_db
from errors import CODE_FORBIDDEN, CODE_UNAUTHORIZED, BizError

COOKIE_NAME = "dsh_token"
TOKEN_TTL_SECONDS = 8 * 3600        # 8 小时，一个工作日
PBKDF2_ITERATIONS = 260_000
ALGO = "HS256"


# ==============================================================================
# 密钥：优先环境变量，其次落盘到 .jwt_secret，避免每次重启让所有人掉线
# ==============================================================================
def _load_secret() -> str:
    env = os.environ.get("JWT_SECRET")
    if env:
        return env

    path = Path(__file__).with_name(".jwt_secret")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    return secret


JWT_SECRET = _load_secret()


# ==============================================================================
# 密码
# ==============================================================================
def hash_password(password: str) -> str:
    """生成 pbkdf2_sha256 哈希串。盐每次随机，同一密码两次结果不同。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    校验密码。

    用 hmac.compare_digest 而不是 ==：字符串比较会短路，
    理论上可以通过响应时间差逐位猜出哈希，虽然不现实但没理由留这个口子。
    """
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


# ==============================================================================
# JWT
# ==============================================================================
def create_token(user: Dict[str, Any]) -> str:
    """签发令牌。ver 取自账号的 token_version，改密后旧令牌自动失效。"""
    now = int(__import__("time").time())
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "name": user["name"],
        "ver": int(user.get("token_version", 1)),
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGO)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """解析并验签。过期、篡改、格式错都返回 None，不向上抛异常。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None


# ==============================================================================
# 当前用户
# ==============================================================================
def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    """
    从 Cookie 解析当前用户。

    注意这里回查了一次数据库：令牌本身是自包含的，
    但如果管理员禁用了账号或对方改了密码，旧令牌仍然"有效"——
    靠比对 token_version 和 status 才能挡住这种情况。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
    if not token:
        return None

    payload = decode_token(token)
    if not payload:
        return None

    user = admin_db.get_user_by_id(payload.get("sub", ""))
    if not user:
        return None
    if user["status"] != "active":
        return None
    if int(payload.get("ver", 0)) != int(user["token_version"]):
        return None

    return {
        "id": user["id"],
        "employee_no": user["employee_no"],
        "name": user["name"],
        "role": user["role"],
    }


def require_login(request: Request) -> Dict[str, Any]:
    """依赖：必须登录，否则 40101。"""
    user = get_current_user(request)
    if not user:
        raise BizError(CODE_UNAUTHORIZED, "登录已过期，请重新登录", 401)
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    """依赖：必须是管理员，否则 40103（员工能登录但进不了后台）。"""
    user = require_login(request)
    if user["role"] != "admin":
        raise BizError(CODE_FORBIDDEN, "无权限访问管理后台", 403)
    return user


# ==============================================================================
# Cookie
# ==============================================================================
def set_auth_cookie(response: Response, token: str) -> None:
    """
    写 Cookie。

    HttpOnly：JS 读不到，XSS 偷不走。
    SameSite=Lax：跨站 POST 不携带，挡住绝大多数 CSRF。
    Secure：仅 HTTPS 下发，本地 http 调试时不能开，用环境变量控制。
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ==============================================================================
# CSRF：Origin 校验
# ==============================================================================
def check_origin(request: Request) -> None:
    """
    对变更类请求（POST/PUT/DELETE）校验 Origin。

    规则：
      - 带了 Origin 且与当前 Host 不一致 → 拒绝（典型的跨站请求伪造）
      - 没带 Origin（curl、服务端调用）→ 放行

    为什么不用 CSRF Token：SameSite=Lax 已经拦住浏览器发起的跨站 POST，
    再加 Origin 校验基本没有漏网场景，而 Token 方案要在每个表单里埋隐藏域、
    每个 fetch 里带 header，维护成本明显更高。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    origin = request.headers.get("origin")
    if not origin:
        return

    host = request.headers.get("host", "")
    if host and host not in origin:
        raise BizError(CODE_FORBIDDEN, "请求来源不合法", 403)
