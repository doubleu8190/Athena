"""Cookie 登录态解析与同源请求校验。"""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from domain.auth import AuthContext, Authenticator

NOT_AUTHENTICATED = "not_authenticated"
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
DEFAULT_COOKIE_NAME = "athena_session"


def _normalize_origin(value: str) -> str | None:
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


def _request_origin(request: Request) -> str | None:
    """从当前请求 URL 生成同源比较值。"""
    return _normalize_origin(f"{request.url.scheme}://{request.url.netloc}")


def verify_request_origin(request: Request) -> None:
    """拒绝带不匹配 Origin 的浏览器写请求。"""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    supplied = request.headers.get("origin")
    if supplied is None:
        return
    allowed = {origin for origin in (_request_origin(request),) if origin is not None}
    configured = cast(str | None, getattr(request.app.state, "public_origin", None))
    if configured:
        configured_origin = _normalize_origin(configured)
        if configured_origin is not None:
            allowed.add(configured_origin)
    submitted_origin = _normalize_origin(supplied)
    if submitted_origin is None or submitted_origin not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ORIGIN_NOT_ALLOWED,
        )


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
    context = await authenticator.authenticate(token)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=NOT_AUTHENTICATED,
        )
    return context


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


__all__ = [
    "CurrentAuth",
    "DEFAULT_COOKIE_NAME",
    "NOT_AUTHENTICATED",
    "ORIGIN_NOT_ALLOWED",
    "get_current_auth",
    "verify_request_origin",
]
