"""全局前置同源校验与登录态鉴权中间件。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import cast

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import cookie_parser

from api.security import (
    DEFAULT_COOKIE_NAME,
    NOT_AUTHENTICATED,
    ORIGIN_NOT_ALLOWED,
    PRE_AUTH_AUTHENTICATOR_STATE_KEY,
    PRE_AUTH_CONTEXT_STATE_KEY,
    PRE_AUTH_TOKEN_STATE_KEY,
    get_current_auth,
    is_origin_allowed,
    normalize_origin,
    require_admin,
    require_skill_manager,
    verify_request_origin,
)
from domain.auth import Authenticator, UserRole

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/v1"
_ADMIN_PREFIX = "/api/v1/admin"
# /api/v1/admin/skills 面向超管与部门管理员开放，由 SkillService 与权限引擎执行细粒度部门范围鉴权。
_SKILL_MANAGER_PREFIX = "/api/v1/admin/skills"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_AUTH_ROUTES = frozenset(
    {
        ("GET", "/api/v1/auth/providers"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/feishu/authorize"),
        ("GET", "/api/v1/auth/feishu/callback"),
        ("GET", "/api/v1/auth/mock/users"),
        ("POST", "/api/v1/auth/mock/login"),
    }
)


def _is_path_under_prefix(path: str, prefix: str) -> bool:
    """判断请求路径是否命中指定前缀或其子路径。"""
    return path == prefix or path.startswith(f"{prefix}/")


def _iter_headers(scope: Mapping[str, object]) -> Iterable[tuple[bytes, bytes]]:
    """安全遍历 ASGI scope 中的原始请求头二元组。"""
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, Iterable):
        return ()
    pairs: list[tuple[bytes, bytes]] = []
    for item in raw_headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        name, value = item
        if isinstance(name, bytes) and isinstance(value, bytes):
            pairs.append((name, value))
    return pairs


def _extract_cookie_token(scope: Mapping[str, object], cookie_name: str) -> str | None:
    """复用 Starlette 同源 cookie_parser 按序合并 Cookie 头，确保与 Request.cookies 口径一致。"""
    cookies: dict[str, str] = {}
    for name, value in _iter_headers(scope):
        if name.lower() != b"cookie":
            continue
        # 与 Starlette HTTPConnection.cookies 口径一致：按头顺序更新，容忍畸形前置段且遵守尾段覆盖。
        cookies.update(cookie_parser(value.decode("latin-1")))
    return cookies.get(cookie_name) or None


def _extract_origin_headers(scope: Mapping[str, object]) -> list[str]:
    """提取请求中携带的全部 Origin 请求头值。"""
    return [
        value.decode("latin-1") for name, value in _iter_headers(scope) if name.lower() == b"origin"
    ]


def _resolve_scope_request_origin(scope: Mapping[str, object]) -> str | None:
    """按照与 Starlette Request.url 一致的规则从 ASGI scope 推导请求自身 origin。"""
    scheme = str(scope.get("scheme", "http"))
    for name, value in _iter_headers(scope):
        if name.lower() == b"host":
            host_origin = normalize_origin(f"{scheme}://{value.decode('latin-1')}")
            if host_origin is not None:
                return host_origin
            break
    server = scope.get("server")
    if isinstance(server, tuple) and len(server) == 2:
        host, port = server
        if isinstance(host, str) and host:
            formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            netloc = f"{formatted_host}:{port}" if isinstance(port, int) else formatted_host
            return normalize_origin(f"{scheme}://{netloc}")
    return None


async def _send_json(send: object, status: int, detail: str) -> None:
    """发送统一结构的 JSON 错误响应。"""
    assert callable(send)
    payload = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class AuthPrecheckMiddleware:
    """在上传准入、请求体解析和路由分发之前统一执行同源校验与登录态鉴权。"""

    def __init__(
        self,
        app: object,
        *,
        fastapi_app: FastAPI | None = None,
        authenticator: Authenticator | None = None,
        cookie_name: str | None = None,
        public_origin: str | None = None,
    ) -> None:
        self.app = app
        self._fastapi_app = fastapi_app
        self._authenticator = authenticator
        self._cookie_name = cookie_name
        self._public_origin = public_origin

    def _resolve_public_origin(self) -> str | None:
        if self._public_origin is not None:
            return self._public_origin
        if self._fastapi_app is not None:
            return cast(str | None, getattr(self._fastapi_app.state, "public_origin", None))
        return None

    def _resolve_authenticator(self) -> Authenticator | None:
        if self._authenticator is not None:
            return self._authenticator
        if self._fastapi_app is not None:
            return cast(
                Authenticator | None,
                getattr(self._fastapi_app.state, "authenticator", None),
            )
        return None

    def _resolve_cookie_name(self) -> str:
        if self._cookie_name is not None:
            return self._cookie_name
        if self._fastapi_app is not None:
            raw = getattr(self._fastapi_app.state, "auth_cookie_name", DEFAULT_COOKIE_NAME)
            if isinstance(raw, str) and raw:
                return raw
        return DEFAULT_COOKIE_NAME

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        scope_type = scope.get("type")
        if scope_type is not None and scope_type != "http":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "")).upper()
        if not _is_path_under_prefix(path, _API_PREFIX) or method == "OPTIONS":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        # 清理可能残留的预鉴权缓存，防止人工复用的测试 scope 携带旧上下文。
        existing_state = scope.get("state")
        if isinstance(existing_state, dict):
            existing_state.pop(PRE_AUTH_CONTEXT_STATE_KEY, None)
            existing_state.pop(PRE_AUTH_TOKEN_STATE_KEY, None)
            existing_state.pop(PRE_AUTH_AUTHENTICATOR_STATE_KEY, None)

        overrides = self._fastapi_app.dependency_overrides if self._fastapi_app is not None else {}

        # 第一道防线：写请求先做同源 Origin 校验，阻断跨站请求触达数据库或公开认证路由。
        if method not in _SAFE_METHODS and verify_request_origin not in overrides:
            origin_headers = _extract_origin_headers(scope)
            if origin_headers:
                request_origin = _resolve_scope_request_origin(scope)
                configured_public_origin = self._resolve_public_origin()
                if any(
                    not is_origin_allowed(
                        method=method,
                        supplied_origin=supplied,
                        request_origin=request_origin,
                        configured_public_origin=configured_public_origin,
                    )
                    for supplied in origin_headers
                ):
                    await _send_json(send, 403, ORIGIN_NOT_ALLOWED)
                    return

        # 公开认证白名单通过 Origin 校验后直接放行，保持登录、SSO/Mock 回调与幂等登出语义。
        if (method, path) in _PUBLIC_AUTH_ROUTES:
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        is_admin_path = _is_path_under_prefix(path, _ADMIN_PREFIX)
        is_skill_manager_path = _is_path_under_prefix(path, _SKILL_MANAGER_PREFIX)
        # 兼容测试中通过 FastAPI dependency_overrides 注入伪登录态或伪管理员依赖。
        if (
            get_current_auth in overrides
            or (is_admin_path and require_admin in overrides)
            or (is_skill_manager_path and require_skill_manager in overrides)
        ):
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        # 未装配认证器（例如局部测试应用）时交由下游路由依赖自行处理。
        authenticator = self._resolve_authenticator()
        if authenticator is None:
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        cookie_name = self._resolve_cookie_name()
        token = _extract_cookie_token(scope, cookie_name)
        if not token:
            await _send_json(send, 401, NOT_AUTHENTICATED)
            return

        try:
            context = await authenticator.authenticate(token)
        except (SQLAlchemyError, OSError):
            logger.error("database_unavailable path=%s", path)
            await _send_json(send, 503, "database_unavailable")
            return

        if context is None:
            await _send_json(send, 401, NOT_AUTHENTICATED)
            return

        if is_skill_manager_path:
            # 技能管理端允许超管与部门管理员进入，普通成员在此前置拒绝。
            if context.user.role not in {UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN}:
                await _send_json(send, 403, "admin_required")
                return
        elif is_admin_path and context.user.role is not UserRole.SUPER_ADMIN:
            await _send_json(send, 403, "admin_required")
            return

        # 将本次鉴权上下文写入 scope.state，供下游 CurrentAuth 依赖直接复用，避免二次查库。
        raw_state = scope.get("state")
        if not isinstance(raw_state, dict):
            raw_state = {}
            scope["state"] = raw_state
        raw_state[PRE_AUTH_CONTEXT_STATE_KEY] = context
        raw_state[PRE_AUTH_TOKEN_STATE_KEY] = token
        raw_state[PRE_AUTH_AUTHENTICATOR_STATE_KEY] = authenticator

        await self.app(scope, receive, send)  # type: ignore[operator]


__all__ = ["AuthPrecheckMiddleware", "_extract_cookie_token"]
