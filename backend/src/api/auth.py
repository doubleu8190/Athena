"""企业身份认证 HTTP 接口（本地应急登录、飞书原生 SSO 与非生产 Mock 快捷登录）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from api.converter.auth import mock_users_to_response, user_result_to_response
from api.dto.auth import (
    AuthProvidersResponse,
    ChangePasswordRequest,
    LoginRequest,
    MockLoginRequest,
    MockUserListResponse,
    UpdateCurrentUserRequest,
    UserResponse,
)
from api.security import (
    NOT_AUTHENTICATED,
    CurrentAuth,
)
from app.auth import (
    AuthProviderDisabledError,
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidOAuthStateError,
    InvalidPasswordError,
    InvalidReturnToError,
    InvalidSessionError,
    MockUserNotFoundError,
    OAuthStateUnavailableError,
    PasswordChangeNotSupportedError,
    RateLimitExceededError,
    RateLimitUnavailableError,
    SsoAuthenticationError,
    UserDisabledError,
)

INVALID_EMAIL = "invalid_email"
INVALID_PASSWORD = "password_invalid"
INVALID_DISPLAY_NAME = "display_name_invalid"
EMAIL_EXISTS = "email_exists"
INVALID_CREDENTIALS = "invalid_credentials"
INVALID_CURRENT_PASSWORD = "current_password_invalid"
PASSWORD_CHANGE_NOT_SUPPORTED = "password_change_not_supported"
RATE_LIMITED = "rate_limited"
RATE_LIMIT_UNAVAILABLE = "rate_limit_unavailable"
INVALID_RETURN_TO = "invalid_return_to"
INVALID_OAUTH_STATE = "invalid_oauth_state"
INVALID_OAUTH_CALLBACK = "invalid_oauth_callback"
OAUTH_STATE_UNAVAILABLE = "oauth_state_unavailable"
FEISHU_AUTH_FAILED = "feishu_auth_failed"
USER_DISABLED = "user_disabled"
MOCK_USER_NOT_FOUND = "mock_user_not_found"


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
    if isinstance(error, AuthProviderDisabledError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
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
    if isinstance(error, PasswordChangeNotSupportedError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=PASSWORD_CHANGE_NOT_SUPPORTED,
        ) from error
    if isinstance(error, InvalidReturnToError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_RETURN_TO,
        ) from error
    if isinstance(error, InvalidOAuthStateError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_OAUTH_STATE,
        ) from error
    if isinstance(error, UserDisabledError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=USER_DISABLED,
        ) from error
    if isinstance(error, MockUserNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=MOCK_USER_NOT_FOUND,
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
    if isinstance(error, RateLimitUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=RATE_LIMIT_UNAVAILABLE,
        ) from error
    if isinstance(error, OAuthStateUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=OAUTH_STATE_UNAVAILABLE,
        ) from error
    if isinstance(error, SsoAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=FEISHU_AUTH_FAILED,
        ) from error
    raise error


def build_auth_router(
    service: AuthService,
    *,
    cookie: AuthCookieConfig,
) -> APIRouter:
    """绑定认证用例并创建同源写请求受保护的认证路由（已关闭开放自助注册）。"""
    router = APIRouter(
        prefix="/api/v1/auth",
        tags=["auth"],
    )

    @router.get("/providers", response_model=AuthProvidersResponse)
    async def providers(response: Response) -> AuthProvidersResponse:
        """返回当前部署开启的认证入口（飞书 SSO、本地应急登录、非生产 Mock）。"""
        response.headers["Cache-Control"] = "no-store"
        return AuthProvidersResponse(
            feishu_enabled=service.feishu_enabled,
            local_login_enabled=True,
            mock_enabled=service.mock_enabled,
        )

    @router.post("/login", response_model=UserResponse)
    async def login(
        request: LoginRequest,
        http_request: Request,
        response: Response,
    ) -> UserResponse:
        """仅允许活跃本地账号校验邮箱密码并建立固定期限会话。"""
        try:
            issued = await service.login(
                request.email,
                request.password,
                remember_me=request.remember_me,
                client_key=_client_key(http_request),
            )
        except (
            InvalidCredentialsError,
            RateLimitExceededError,
            RateLimitUnavailableError,
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

    @router.get(
        "/feishu/authorize",
        status_code=status.HTTP_302_FOUND,
        response_class=RedirectResponse,
    )
    async def feishu_authorize(return_to: str | None = None) -> RedirectResponse:
        """校验站内回跳路径并写入一次性 state，302 跳转至飞书 OAuth 授权页。"""
        try:
            authorize_url = await service.start_feishu_authorize(return_to=return_to)
        except (
            AuthProviderDisabledError,
            InvalidReturnToError,
            OAuthStateUnavailableError,
        ) as error:
            _raise_auth_error(error)
        return RedirectResponse(
            url=authorize_url,
            status_code=status.HTTP_302_FOUND,
            headers={"Cache-Control": "no-store"},
        )

    @router.get(
        "/feishu/callback",
        status_code=status.HTTP_302_FOUND,
        response_class=RedirectResponse,
    )
    async def feishu_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """原子消费 state、换取飞书用户身份、同步本地记录并签发同源 Cookie 后 302 跳转。"""
        if not service.feishu_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="feishu_not_enabled",
            )
        if error is not None or not code or not state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_OAUTH_CALLBACK,
            )
        try:
            issued, return_to = await service.complete_feishu_callback(
                code=code,
                state=state,
            )
        except (
            AuthProviderDisabledError,
            InvalidOAuthStateError,
            InvalidReturnToError,
            OAuthStateUnavailableError,
            SsoAuthenticationError,
            UserDisabledError,
        ) as exc:
            _raise_auth_error(exc)
        redirect = RedirectResponse(
            url=return_to,
            status_code=status.HTTP_302_FOUND,
        )
        _set_session_cookie(
            redirect,
            token=issued.token,
            max_age_seconds=issued.max_age_seconds,
            config=cookie,
        )
        redirect.headers["Cache-Control"] = "no-store"
        return redirect

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
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
        """返回当前有效登录用户的安全资料、所属部门与管辖部门 ID。"""
        response.headers["Cache-Control"] = "no-store"
        enriched = await service.get_me(current_auth)
        return user_result_to_response(enriched)

    @router.patch("/me", response_model=UserResponse)
    async def update_me(
        request: UpdateCurrentUserRequest,
        current_auth: CurrentAuth,
        response: Response,
    ) -> UserResponse:
        """更新当前用户显示名并返回最新身份资料。"""
        try:
            updated_user = await service.update_display_name(
                current_auth,
                request.display_name,
            )
        except (InvalidDisplayNameError, InvalidSessionError) as error:
            _raise_auth_error(error)
        response.headers["Cache-Control"] = "no-store"
        return user_result_to_response(updated_user)

    @router.post(
        "/password",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def change_password(
        request: ChangePasswordRequest,
        current_auth: CurrentAuth,
    ) -> Response:
        """仅允许本地密码账号修改密码并撤销其他设备会话。"""
        try:
            await service.change_password(
                current_auth,
                request.current_password,
                request.new_password,
            )
        except (
            InvalidPasswordError,
            InvalidCurrentPasswordError,
            PasswordChangeNotSupportedError,
            InvalidSessionError,
            RateLimitExceededError,
            RateLimitUnavailableError,
        ) as error:
            _raise_auth_error(error)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers["Cache-Control"] = "no-store"
        return response

    # 仅在非生产且显式开启 ATHENA_AUTH_MOCK_ENABLED=true 时挂载 Mock 快捷登录路由。
    if service.mock_enabled:

        @router.get("/mock/users", response_model=MockUserListResponse)
        async def mock_users(response: Response) -> MockUserListResponse:
            """返回非生产环境可用的 Mock 多角色测试账号清单。"""
            try:
                items = await service.list_mock_users()
            except AuthProviderDisabledError as error:
                _raise_auth_error(error)
            response.headers["Cache-Control"] = "no-store"
            return mock_users_to_response(items)

        @router.post("/mock/login", response_model=UserResponse)
        async def mock_login(
            request: MockLoginRequest,
            response: Response,
        ) -> UserResponse:
            """校验目标为活跃 Mock 账号后签发真实服务端会话与 HttpOnly Cookie。"""
            try:
                issued = await service.login_mock_user(request.user_id)
            except (
                AuthProviderDisabledError,
                MockUserNotFoundError,
                InvalidCredentialsError,
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

    return router


__all__ = ["AuthCookieConfig", "build_auth_router"]
