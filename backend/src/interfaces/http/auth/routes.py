"""Single-user signed cookie authentication routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from bootstrap.config import Settings, get_settings

SESSION_COOKIE_NAME = "athena_session"


class LoginRequest(BaseModel):
    username: str
    password: str


def _signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _token(username: str, secret: str, ttl: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{payload}.{_signature(payload, secret)}"


def is_authenticated(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.auth_enabled:
        return True
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if "." not in raw or not settings.auth_session_secret:
        return False
    body, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature, _signature(body, settings.auth_session_secret)):
        return False
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("exp", 0) > time.time()
    except Exception:
        return False


def build_auth_router(settings_factory: Callable[[], Settings] | None = None) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login")
    async def login(body: LoginRequest, response: Response, request: Request) -> dict[str, str]:
        settings = (settings_factory or get_settings)()
        if not hmac.compare_digest(body.username, settings.auth_username) or not hmac.compare_digest(body.password, settings.auth_password):
            raise HTTPException(status_code=401, detail="invalid_credentials")
        if settings.auth_enabled and len(settings.auth_session_secret) < 32:
            raise HTTPException(status_code=503, detail="auth_secret_not_configured")
        response.set_cookie(
            SESSION_COOKIE_NAME,
            _token(body.username, settings.auth_session_secret or "disabled", settings.auth_session_ttl_hours * 3600),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=settings.auth_session_ttl_hours * 3600,
            path="/",
        )
        return {"status": "ok"}

    @router.post("/logout")
    async def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"status": "ok"}

    return router


__all__ = ["LoginRequest", "SESSION_COOKIE_NAME", "build_auth_router", "is_authenticated"]
