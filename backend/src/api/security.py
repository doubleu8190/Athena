"""Cookie 登录态解析与同源请求校验。"""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from domain.auth import AuthContext, Authenticator, UserRole

NOT_AUTHENTICATED = "not_authenticated"
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
DEFAULT_COOKIE_NAME = "athena_session"
# 单次请求内复用前置鉴权中间件解析结果的状态键，避免路由依赖重复查库。
PRE_AUTH_CONTEXT_STATE_KEY = "_pre_auth_context"
PRE_AUTH_TOKEN_STATE_KEY = "_pre_auth_token"
PRE_AUTH_AUTHENTICATOR_STATE_KEY = "_pre_auth_authenticator"


def normalize_origin(value: str) -> str | None:
    """把 http(s) origin 规范化为小写 scheme、host 和可选端口。"""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        default_port = 80 if parsed.scheme.lower() == "http" else 443
        netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
        return f"{parsed.scheme.lower()}://{netloc}"
    except ValueError:
        return None


def is_origin_allowed(
    *,
    method: str,
    supplied_origin: str | None,
    request_origin: str | None,
    configured_public_origin: str | None,
) -> bool:
    """判断写请求 Origin 是否与请求源或公开源同源（由 AuthPrecheckMiddleware 统一调用）。"""
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    if supplied_origin is None:
        return True
    allowed = {
        origin
        for origin in (
            normalize_origin(request_origin) if request_origin is not None else None,
            normalize_origin(configured_public_origin) if configured_public_origin else None,
        )
        if origin is not None
    }
    submitted_origin = normalize_origin(supplied_origin)
    return submitted_origin is not None and submitted_origin in allowed


async def get_current_auth(request: Request) -> AuthContext:
    """从 HttpOnly Cookie 读取并验证当前登录上下文。"""
    authenticator = cast(
        Authenticator | None,
        getattr(request.app.state, "authenticator", None),
    )
    cookie_name = cast(
        str,
        getattr(request.app.state, "auth_cookie_name", DEFAULT_COOKIE_NAME),
    )
    token = request.cookies.get(cookie_name)
    if authenticator is None or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=NOT_AUTHENTICATED,
        )
    # 命中外层 AuthPrecheckMiddleware 在同一次请求内写入的缓存时直接复用，消除重复查库。
    cached_context = getattr(request.state, PRE_AUTH_CONTEXT_STATE_KEY, None)
    cached_token = getattr(request.state, PRE_AUTH_TOKEN_STATE_KEY, None)
    cached_authenticator = getattr(request.state, PRE_AUTH_AUTHENTICATOR_STATE_KEY, None)
    if (
        isinstance(cached_context, AuthContext)
        and cached_token == token
        and cached_authenticator is authenticator
    ):
        return cached_context
    context = await authenticator.authenticate(token)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=NOT_AUTHENTICATED,
        )
    return context


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


async def require_admin(current_auth: CurrentAuth) -> AuthContext:
    """基于本次数据库认证结果预检查超级管理员角色。

    管理操作服务仍须通过 CurrentSessionReader 与仓储无锁身份复核（verify_active_session_user）
    复核当前有效身份及角色，避免把依赖层检查当作事务内最终授权。此依赖不读取任何管理资源。
    """
    if current_auth.user.role is not UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_required",
        )
    return current_auth


AdminAuth = Annotated[AuthContext, Depends(require_admin)]


async def require_skill_manager(current_auth: CurrentAuth) -> AuthContext:
    """基于本次数据库认证结果预检查超管或部门管理员角色（用于 /api/v1/admin/skills）。

    细粒度部门范围与事务内无锁身份复核仍由 SkillService 与仓储完成。
    """
    if current_auth.user.role not in {UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_required",
        )
    return current_auth


SkillManagerAuth = Annotated[AuthContext, Depends(require_skill_manager)]


__all__ = [
    "CurrentAuth",
    "AdminAuth",
    "SkillManagerAuth",
    "DEFAULT_COOKIE_NAME",
    "NOT_AUTHENTICATED",
    "ORIGIN_NOT_ALLOWED",
    "PRE_AUTH_AUTHENTICATOR_STATE_KEY",
    "PRE_AUTH_CONTEXT_STATE_KEY",
    "PRE_AUTH_TOKEN_STATE_KEY",
    "get_current_auth",
    "is_origin_allowed",
    "normalize_origin",
    "require_admin",
    "require_skill_manager",
]
