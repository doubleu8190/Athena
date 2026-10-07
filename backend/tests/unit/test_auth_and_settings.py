"""Target configuration and authentication contract tests."""

from __future__ import annotations

import base64
import json
import time

from fastapi.testclient import TestClient
from starlette.requests import Request
from fastapi import FastAPI

from backend.src.bootstrap import create_app
from backend.src.bootstrap.config import Settings
from backend.src.interfaces.http.auth.routes import (
    SESSION_COOKIE_NAME,
    build_auth_router,
    is_authenticated,
)
from backend.src.interfaces.http.auth import middleware as auth_middleware
from backend.src.interfaces.http.auth import routes as auth_routes


def test_target_settings_keep_database_and_provider_contract() -> None:
    settings = Settings(
        llm_providers=[{"name": "primary", "provider": "openai", "model": "gpt-4o"}],
        postgres_user="user",
        postgres_password="p@ss",
        postgres_host="db",
        postgres_db="athena_test",
        postgres_port=5433,
    )
    assert settings.primary_llm.model == "gpt-4o"
    assert settings.postgres_url == "postgresql+psycopg://user:p%40ss@db:5433/athena_test"
    assert settings.postgres_conn_string.startswith("postgresql://user:")


def test_auth_routes_issue_and_revoke_signed_cookie() -> None:
    settings = Settings(
        auth_enabled=True,
        auth_username="alice",
        auth_password="secret",
        auth_session_secret="x" * 32,
    )
    app = create_app()
    app.router.routes = [route for route in app.router.routes if not getattr(route, "path", "").startswith("/api/auth")]
    app.include_router(build_auth_router(lambda: settings), prefix="/api")
    client = TestClient(app)

    assert client.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
    response = client.post("/api/auth/login", json={"username": "alice", "password": "secret"})
    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.cookies
    raw_cookie = response.cookies.get(SESSION_COOKIE_NAME)
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={raw_cookie}".encode())],
        }
    )
    assert is_authenticated(request, settings) is True
    assert client.post("/api/auth/logout").status_code == 200


def test_default_app_exposes_compatibility_health_and_auth_paths() -> None:
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/health", "/api/health", "/api/auth/login", "/api/auth/logout"} <= paths


def _cookie_request(value: str) -> Request:
    return Request({"type": "http", "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={value}".encode())]})


def test_auth_rejects_bad_signature_expired_and_invalid_payload() -> None:
    settings = Settings(auth_enabled=True, auth_username="athena", auth_password="change-me", auth_session_secret="s" * 32)
    assert is_authenticated(_cookie_request("bad"), settings) is False
    assert is_authenticated(_cookie_request("payload.invalid"), settings) is False
    expired = base64.urlsafe_b64encode(
        json.dumps({"u": "alice", "exp": int(time.time()) - 10}).encode()
    ).decode().rstrip("=")
    import hashlib
    import hmac

    signature = hmac.new(settings.auth_session_secret.encode(), expired.encode(), hashlib.sha256).hexdigest()
    assert is_authenticated(_cookie_request(f"{expired}.{signature}"), settings) is False


def test_auth_login_requires_strong_secret_when_enabled() -> None:
    settings = Settings(auth_enabled=True, auth_username="alice", auth_password="secret", auth_session_secret="short")
    app = FastAPI()
    app.include_router(build_auth_router(lambda: settings), prefix="/api")
    response = TestClient(app).post("/api/auth/login", json={"username": "alice", "password": "secret"})
    assert response.status_code == 503


def test_auth_middleware_blocks_unauthenticated_and_invalid_origins(monkeypatch) -> None:
    settings = Settings(
        auth_enabled=True,
        auth_username="athena",
        auth_password="change-me",
        auth_session_secret="s" * 32,
    )
    monkeypatch.setattr(auth_middleware, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = create_app()
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody", "password": "bad"}, headers={"origin": "https://attacker.invalid"}).status_code == 401

    response = client.post("/api/auth/login", json={"username": "athena", "password": "change-me"})
    assert response.status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200


def test_auth_middleware_rejects_cross_origin_write_with_valid_cookie(monkeypatch) -> None:
    settings = Settings(auth_enabled=True, auth_username="alice", auth_password="secret", auth_session_secret="s" * 32)
    monkeypatch.setattr(auth_middleware, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = create_app()
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "alice", "password": "secret"}).status_code == 200
    response = client.post("/api/auth/logout", headers={"origin": "https://attacker.invalid"})
    assert response.status_code == 200
    response = client.post("/api/auth/login", json={"username": "alice", "password": "secret"})
    assert response.status_code == 200
    response = client.post("/protected", headers={"origin": "https://attacker.invalid"})
    assert response.status_code == 403
