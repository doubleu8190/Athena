"""应用工厂认证组装测试。"""

from __future__ import annotations

import pytest
from auth_support import TEST_PASSWORD, make_auth_service
from fastapi.testclient import TestClient

from main import create_app


def test_create_app_mounts_auth_and_reads_deployment_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_COOKIE_SECURE", "true")
    monkeypatch.setenv("ATHENA_PUBLIC_ORIGIN", "https://app.example.com")

    with TestClient(
        create_app(auth_service=make_auth_service()),
        base_url="https://testserver",
    ) as client:
        rejected = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "https://evil.example.com"},
            json={
                "email": "user@example.com",
                "password": TEST_PASSWORD,
            },
        )
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "https://app.example.com"},
            json={
                "email": "user@example.com",
                "password": TEST_PASSWORD,
            },
        )
        me = client.get("/api/v1/auth/me")

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "origin_not_allowed"}
    assert registered.status_code == 201
    assert "secure" in registered.headers["set-cookie"].lower()
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


def test_create_app_rejects_invalid_cookie_secure_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_COOKIE_SECURE", "sometimes")

    with pytest.raises(ValueError, match="ATHENA_COOKIE_SECURE_invalid"):
        create_app(auth_service=make_auth_service())


def test_create_app_without_services_only_mounts_health() -> None:
    with TestClient(create_app()) as client:
        health = client.get("/health")
        auth = client.get("/api/v1/auth/me")
        projects = client.get("/api/v1/projects")

    assert health.status_code == 200
    assert auth.status_code == 404
    assert projects.status_code == 404
