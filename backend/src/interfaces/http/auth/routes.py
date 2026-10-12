"""认证 HTTP 路由，只负责请求解析和 Cookie 传输。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from application.auth import AuthenticationConfig, AuthenticationService, InvalidCredentialsError
from configuration import get_settings  # 兼容旧调用方；bootstrap 会显式注入 options。
from .options import AuthOptions

SESSION_COOKIE_NAME = "athena_session"


class LoginRequest(BaseModel):
    username: str
    password: str


def _service_from_options(options: AuthOptions) -> AuthenticationService:
    """将 HTTP 适配层配置转换为应用认证服务。"""
    return AuthenticationService(AuthenticationConfig(
        enabled=options.enabled,
        username=options.username,
        password=options.password,
        session_secret=options.session_secret,
        session_ttl_seconds=options.session_ttl_seconds,
    ))


def _resolve_options(value: AuthOptions | Any | None = None) -> AuthOptions:
    """兼容迁移期 Settings 工厂，将其转换为 AuthOptions。"""
    if isinstance(value, AuthOptions):
        return value
    return AuthOptions.from_settings(value or get_settings())


def is_authenticated(request: Request, settings: AuthOptions | Any | None = None) -> bool:
    """验证请求 Cookie；认证关闭时保持开发环境免登录行为。"""
    options = _resolve_options(settings)
    return _service_from_options(options).session(request.cookies.get(SESSION_COOKIE_NAME)).authenticated


def build_auth_router(
    settings_factory: Callable[[], AuthOptions | Any] | None = None,
    options: AuthOptions | None = None,
    auth_service: AuthenticationService | Callable[[], AuthenticationService] | None = None,
) -> APIRouter:
    """创建登录、登出和会话状态路由。"""
    router = APIRouter(prefix="/auth", tags=["auth"])
    current_options = options or (
        AuthOptions(
            enabled=auth_service.config.enabled,
            username=auth_service.config.username,
            password=auth_service.config.password,
            session_secret=auth_service.config.session_secret,
            session_ttl_seconds=auth_service.config.session_ttl_seconds,
        )
        if isinstance(auth_service, AuthenticationService)
        else _resolve_options((settings_factory or _default_settings)())
    )

    def service() -> AuthenticationService:
        if auth_service is None:
            return _service_from_options(current_options)
        return auth_service() if callable(auth_service) and not isinstance(auth_service, AuthenticationService) else auth_service

    @router.post("/login")
    async def login(body: LoginRequest, response: Response, request: Request) -> dict[str, Any]:
        try:
            result = await service().login(body.username, body.password)
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail="invalid_credentials") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        current = service()
        response.set_cookie(
            SESSION_COOKIE_NAME,
            current.issue_token(result.username),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=current.config.session_ttl_seconds,
            path="/",
        )
        return {"status": "ok", "authenticated": result.authenticated, "username": result.username}

    @router.post("/logout")
    async def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"status": "ok"}

    @router.get("/session")
    async def session(request: Request) -> dict[str, Any]:
        result = service().session(request.cookies.get(SESSION_COOKIE_NAME))
        return {"authenticated": result.authenticated, "username": result.username}

    return router


def _default_settings() -> Any:
    """延迟读取默认配置，避免模块导入时产生配置副作用。"""
    return get_settings()


__all__ = ["LoginRequest", "SESSION_COOKIE_NAME", "build_auth_router", "is_authenticated"]
