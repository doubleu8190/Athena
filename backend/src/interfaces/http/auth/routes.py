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

from configuration import get_settings
from .options import AuthOptions

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


def _resolve_options(value: AuthOptions | Any | None = None) -> AuthOptions:
    if isinstance(value, AuthOptions):
        return value
    return AuthOptions.from_settings(value or get_settings())


def is_authenticated(request: Request, settings: AuthOptions | Any | None = None) -> bool:
    options = _resolve_options(settings)
    if not options.enabled:
        return True
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if "." not in raw or not options.session_secret:
        return False
    body, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature, _signature(body, options.session_secret)):
        return False
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("exp", 0) > time.time()
    except Exception:
        return False


def build_auth_router(
    settings_factory: Callable[[], AuthOptions | Any] | None = None,
    options: AuthOptions | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login")
    async def login(body: LoginRequest, response: Response, request: Request) -> dict[str, str]:
        current = options or _resolve_options((settings_factory or get_settings)())
        if not hmac.compare_digest(body.username, current.username) or not hmac.compare_digest(body.password, current.password):
            raise HTTPException(status_code=401, detail="invalid_credentials")
        if current.enabled and len(current.session_secret) < 32:
            raise HTTPException(status_code=503, detail="auth_secret_not_configured")
        response.set_cookie(
            SESSION_COOKIE_NAME,
            _token(body.username, current.session_secret or "disabled", current.session_ttl_seconds),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=current.session_ttl_seconds,
            path="/",
        )
        return {"status": "ok"}

    @router.post("/logout")
    async def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"status": "ok"}

    return router


__all__ = ["LoginRequest", "SESSION_COOKIE_NAME", "build_auth_router", "is_authenticated"]
