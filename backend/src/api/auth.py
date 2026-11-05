"""邮箱密码认证 HTTP 接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from api.converter.auth import user_result_to_response
from api.dto.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UserResponse,
)
from api.security import (
    NOT_AUTHENTICATED,
    CurrentAuth,
    verify_request_origin,
)
from app.auth import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    InvalidSessionError,
    RateLimitExceededError,
)
from app.auth.converter.result import user_to_result

INVALID_EMAIL = "invalid_email"
INVALID_PASSWORD = "password_invalid"
INVALID_DISPLAY_NAME = "display_name_invalid"
EMAIL_EXISTS = "email_exists"
INVALID_CREDENTIALS = "invalid_credentials"
INVALID_CURRENT_PASSWORD = "current_password_invalid"
RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class AuthCookieConfig:
    """认证 Cookie 的部署配置。"""

    name: str = "athena_session"
    secure: bool = False


def _client_key(request: Request) -> str:
    """使用直连客户端地址，不信任可伪造的转发头。"""
    if request.client is None:
        return "unknown"
    return request.client.host


def _set_session_cookie(
    response: Response,
    *,
    token: str,
    max_age_seconds: int,
    config: AuthCookieConfig,
) -> None:
    """设置 HttpOnly 同站 Cookie。"""
    response.set_cookie(
        key=config.name,
        value=token,
        max_age=max_age_seconds,
        path="/",
        secure=config.secure,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, config: AuthCookieConfig) -> None:
    """清除与签发时属性一致的登录 Cookie。"""
    response.delete_cookie(
        key=config.name,
        path="/",
        secure=config.secure,
        httponly=True,
        samesite="lax",
    )


def _raise_auth_error(error: Exception) -> NoReturn:
    """把认证领域异常映射为稳定 HTTP 错误。"""
    if isinstance(error, InvalidEmailError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INVALID_EMAIL,
        ) from error
    if isinstance(error, InvalidPasswordError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INVALID_PASSWORD,
        ) from error
    if isinstance(error, InvalidDisplayNameError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INVALID_DISPLAY_NAME,
        ) from error
    if isinstance(error, EmailAlreadyRegisteredError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=EMAIL_EXISTS,
        ) from error
    if isinstance(error, InvalidCredentialsError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS,
        ) from error
    if isinstance(error, InvalidCurrentPasswordError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CURRENT_PASSWORD,
        ) from error
    if isinstance(error, InvalidSessionError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=NOT_AUTHENTICATED,
        ) from error
    if isinstance(error, RateLimitExceededError):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMITED,
        ) from error
    raise error


def build_auth_router(
    service: AuthService,
    *,
    cookie: AuthCookieConfig,
) -> APIRouter:
    """绑定认证用例并创建同源写请求受保护的公开路由。"""
    router = APIRouter(
        prefix="/api/v1/auth",
        tags=["auth"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.post(
        "/register",
        response_model=UserResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register(
        request: RegisterRequest,
        http_request: Request,
        response: Response,
    ) -> UserResponse:
        """开放注册并直接建立登录态。"""
        try:
            issued = await service.register(
                request.email,
                request.password,
                display_name=request.display_name,
                client_key=_client_key(http_request),
            )
        except (
            InvalidEmailError,
            InvalidPasswordError,
            InvalidDisplayNameError,
            EmailAlreadyRegisteredError,
            RateLimitExceededError,
        ) as error:
            _raise_auth_error(error)
        _set_session_cookie(
            response,
            token=issued.token,
            max_age_seconds=issued.max_age_seconds,
            config=cookie,
        )
        response.headers["Cache-Control"] = "no-store"
        return user_result_to_response(issued.user)

    @router.post("/login", response_model=UserResponse)
    async def login(
        request: LoginRequest,
        http_request: Request,
        response: Response,
    ) -> UserResponse:
        """校验邮箱密码并建立固定期限会话。"""
        try:
            issued = await service.login(
                request.email,
                request.password,
                remember_me=request.remember_me,
                client_key=_client_key(http_request),
            )
        except (InvalidCredentialsError, RateLimitExceededError) as error:
            _raise_auth_error(error)
        _set_session_cookie(
            response,
            token=issued.token,
            max_age_seconds=issued.max_age_seconds,
            config=cookie,
        )
        response.headers["Cache-Control"] = "no-store"
        return user_result_to_response(issued.user)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(http_request: Request) -> Response:
        """撤销当前会话并清除 Cookie。"""
        token = http_request.cookies.get(cookie.name)
        if token:
            await service.logout(token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _clear_session_cookie(response, cookie)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/me", response_model=UserResponse)
    async def me(current_auth: CurrentAuth, response: Response) -> UserResponse:
        """返回当前有效登录用户。"""
        response.headers["Cache-Control"] = "no-store"
        return user_result_to_response(user_to_result(current_auth.user))

    @router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
    async def change_password(
        request: ChangePasswordRequest,
        current_auth: CurrentAuth,
    ) -> Response:
        """修改密码并撤销其他设备会话。"""
        try:
            await service.change_password(
                current_auth,
                request.current_password,
                request.new_password,
            )
        except (
            InvalidPasswordError,
            InvalidCurrentPasswordError,
            InvalidSessionError,
        ) as error:
            _raise_auth_error(error)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


__all__ = ["AuthCookieConfig", "build_auth_router"]
