"""认证和来源校验中间件。"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from application.auth import AuthenticationConfig, AuthenticationService
from configuration import get_settings  # 兼容旧测试；实际配置由 bootstrap 注入。
from .options import AuthOptions
from .routes import SESSION_COOKIE_NAME


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """使用 bootstrap 注入的认证服务保护 HTTP 请求。"""

    def __init__(self, app, *, options: AuthOptions | None = None, auth_service: AuthenticationService | None = None) -> None:
        super().__init__(app)
        if options is None and auth_service is not None:
            options = AuthOptions(
                enabled=auth_service.config.enabled,
                username=auth_service.config.username,
                password=auth_service.config.password,
                session_secret=auth_service.config.session_secret,
                session_ttl_seconds=auth_service.config.session_ttl_seconds,
            )
        self._options = options or AuthOptions()
        self._auth_service = auth_service or AuthenticationService(AuthenticationConfig(
            enabled=self._options.enabled,
            username=self._options.username,
            password=self._options.password,
            session_secret=self._options.session_secret,
            session_ttl_seconds=self._options.session_ttl_seconds,
        ))

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """执行来源校验和 Cookie 验证。"""
        options = self._options
        if _is_worker_service_route(request.url.path):
            # 这些接口由独立 Worker Bearer 凭据认证，不能被浏览器 Cookie
            # 或 CORS Origin 策略提前拒绝；路由本身仍必须调用 WorkerProtocolHttp。
            return await call_next(request)
        public = {"/health", "/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/session"}
        if options.enabled and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in public:
            origin = request.headers.get("origin")
            allowed = {
                f"http://{options.host}:{options.port}",
                f"http://{options.host}:{options.frontend_port}",
                f"http://localhost:{options.frontend_port}",
                f"http://127.0.0.1:{options.frontend_port}",
            }
            if options.frontend_origin:
                allowed.add(options.frontend_origin.rstrip("/"))
            if origin and origin not in allowed:
                return JSONResponse({"detail": "invalid_origin"}, status_code=403)
        if options.enabled and request.url.path not in public:
            if self._auth_service.authenticate_token(request.cookies.get(SESSION_COOKIE_NAME)) is None:
                return JSONResponse({"detail": "authentication_required"}, status_code=401)
        return await call_next(request)


def _is_worker_service_route(path: str) -> bool:
    """判断路径是否严格位于独立 Worker 服务身份保护的命名空间。

    参数：
    - `path`（str）：ASGI 请求 URL 路径。

    返回：仅当路径为协议根或其子路径时返回 `True`。

    异常：此函数不主动抛出业务异常。
    """
    prefixes = (
        "/api/worker-instances",
        "/api/worker-resources",
        "/api/worker-executions",
        "/api/worker-approvals",
        "/worker-api/v1",
    )
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


__all__ = ["AuthenticationMiddleware"]
