"""系统设置/提供商端点测试 — GET /settings, GET /providers, POST /providers/test."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.config.settings import LLMProviderConfig, Settings


@pytest.fixture
def settings():
    return Settings(
        llm_providers=[
            LLMProviderConfig(
                name="primary",
                provider="openai",
                model="gpt-4o",
                api_key="sk-test-1234abcd",
                base_url="https://example.com",
            ),
            LLMProviderConfig(
                name="fallback",
                provider="deepseek",
                model="deepseek-chat",
                api_key="",
            ),
        ],
        host="0.0.0.0",
        sandbox_enabled=True,
    )


@pytest.fixture
def client(settings):
    from athena.gateway.routes.settings import router as settings_router
    from athena.gateway.routes.providers import router as providers_router

    app = FastAPI()
    app.include_router(settings_router)
    app.include_router(providers_router)

    with patch("athena.config.settings.get_settings", return_value=settings):
        yield TestClient(app)


def test_get_settings_readonly(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "0.0.0.0"
    assert body["port"] == 8000
    assert body["sandbox_enabled"] is True
    # 绝不泄露 api_key
    assert "api_key" not in body
    assert "llm_providers" not in body


def test_get_providers_masks_keys(client):
    resp = client.get("/providers")
    assert resp.status_code == 200
    providers = resp.json()
    assert len(providers) == 2

    primary = providers[0]
    assert primary["name"] == "primary"
    assert primary["api_key_configured"] is True
    assert primary["api_key_masked"] == "••••abcd"
    assert "sk-test-1234" not in str(primary)

    fallback = providers[1]
    assert fallback["api_key_configured"] is False
    assert fallback["api_key_masked"] == ""
    assert fallback["model"] == "deepseek-chat"


def test_provider_test_ok(client):
    fake_model = AsyncMock()
    fake_model.ainvoke = AsyncMock(return_value="pong")
    with patch(
        "athena.gateway.routes.providers._create_chat_model",
        return_value=fake_model,
    ):
        resp = client.post(
            "/providers/test",
            json={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-x",
                "base_url": "",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert isinstance(body["latency_ms"], int)


def test_provider_test_error(client):
    def _boom(*args, **kwargs):
        raise ValueError("Unsupported LLM provider")

    with patch(
        "athena.gateway.routes.providers._create_chat_model",
        side_effect=_boom,
    ):
        resp = client.post(
            "/providers/test",
            json={
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "sk-x",
                "base_url": "",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Unsupported LLM provider" in body["error"]
