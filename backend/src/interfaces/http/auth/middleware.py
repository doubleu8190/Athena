"""Authentication and origin checks for the target HTTP interface."""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from configuration import get_settings
from .options import AuthOptions
from .routes import is_authenticated


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, options: AuthOptions | None = None) -> None:
        super().__init__(app)
        self._options = options

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        options = self._options or AuthOptions.from_settings(get_settings())
        public = {"/health", "/api/health", "/api/auth/login", "/api/auth/logout"}
        if options.enabled and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in public:
            origin = request.headers.get("origin")
            allowed = {f"http://{options.host}:{options.port}", "http://localhost:5173", "http://127.0.0.1:5173"}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "invalid_origin"}, status_code=403)
        if options.enabled and request.url.path not in public and not is_authenticated(request, options):
            return JSONResponse({"detail": "authentication_required"}, status_code=401)
        return await call_next(request)


__all__ = ["AuthenticationMiddleware"]
