"""HTTP 认证中间件。

认证策略属于 Gateway 层：应用入口只负责组装中间件，具体的请求来源和
会话校验逻辑集中在这里，避免把 HTTP 安全规则混入应用生命周期代码。
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from athena.config.settings import get_settings
from athena.contracts.errors import ErrorDetail
from athena.gateway.auth.routes import is_authenticated


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """校验写请求来源和受保护接口的认证状态。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """执行来源校验和 Cookie 会话校验。

        参数：
            request (Request): 当前 HTTP 请求。
            call_next (RequestResponseEndpoint): 下一个中间件或路由处理器。
        返回值：
            Response: 下游响应，或认证失败时的 JSON 错误响应。
        异常：
            下游处理器抛出的异常由中间件链继续传播。
        """
        settings = get_settings()
        if (
            settings.auth_enabled
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path not in {"/api/auth/login", "/api/auth/logout"}
        ):
            origin = request.headers.get("origin")
            if origin and origin not in {
                f"http://{settings.host}:{settings.port}",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            }:
                return JSONResponse(
                    {"detail": ErrorDetail.INVALID_ORIGIN}, status_code=403
                )
        if (
            settings.auth_enabled
            and request.url.path
            not in {"/api/auth/login", "/api/auth/logout", "/api/health", "/health"}
            and not is_authenticated(request)
        ):
            return JSONResponse(
                {"detail": ErrorDetail.AUTHENTICATION_REQUIRED}, status_code=401
            )
        return await call_next(request)
