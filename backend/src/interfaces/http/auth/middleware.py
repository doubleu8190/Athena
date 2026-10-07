"""Authentication and origin checks for the target HTTP interface."""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from bootstrap.config import get_settings
from .routes import is_authenticated


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        public = {"/health", "/api/health", "/api/auth/login", "/api/auth/logout"}
        if settings.auth_enabled and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in public:
            origin = request.headers.get("origin")
            allowed = {f"http://{settings.host}:{settings.port}", "http://localhost:5173", "http://127.0.0.1:5173"}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "invalid_origin"}, status_code=403)
        if settings.auth_enabled and request.url.path not in public and not is_authenticated(request, settings):
            return JSONResponse({"detail": "authentication_required"}, status_code=401)
        return await call_next(request)


__all__ = ["AuthenticationMiddleware"]
