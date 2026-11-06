"""验证应用组装后的 HTTP 存活检查。"""

from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import main
from app.memory.service import MemoryService
from app.tool import ToolManager
from main import create_app


def test_health() -> None:
    """通过真实路由检查响应状态与内容。

    参数：无。
    返回：None。
    异常：AssertionError，响应不符合约定时抛出。
    """
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_llm_is_optional_without_profiles() -> None:
    """未配置 Profile 时不创建 LLM 服务且健康检查仍可用。"""
    app = create_app()
    assert app.state.llm_service is None
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_memory_is_disabled_by_default_and_can_be_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_MEMORY_ENABLED", raising=False)
    disabled_app = create_app()
    assert disabled_app.state.memory_service is None

    service = cast(MemoryService, object())
    injected_app = create_app(memory_service=service)
    assert injected_app.state.memory_service is service


def test_injected_llm_service_ignores_invalid_environment_and_keeps_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入服务不解析部署配置，也不关闭调用方持有的资源。"""

    class InjectedService:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    service = InjectedService()
    monkeypatch.setenv("ATHENA_LLM_PROFILES", "not-json")
    app = create_app(llm_service=service)  # type: ignore[arg-type]
    assert app.state.llm_service is service
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert not service.closed


def test_self_built_llm_adapter_closes_on_app_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """应用自建的 LLM adapter 在 lifespan 退出时关闭。"""
    closed: list[bool] = []

    async def close(self: Any) -> None:
        closed.append(True)

    monkeypatch.setenv(
        "ATHENA_LLM_PROFILES", '{"test":{"api_key_env":"TEST_KEY","model":"m","capabilities":{}}}'
    )
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setattr("main.OpenAIResponsesAdapter.aclose", close)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
    assert closed == [True]


def test_llm_close_failure_does_not_prevent_database_disposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 资源关闭异常时仍释放 lifespan 中的数据库资源。"""
    disposed: list[bool] = []

    class FakeDatabase:
        session_factory = object()

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def dispose(self) -> None:
            disposed.append(True)

    async def fail_close(self: Any) -> None:
        raise RuntimeError("safe fake")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv(
        "ATHENA_LLM_PROFILES", '{"test":{"api_key_env":"TEST_KEY","model":"m","capabilities":{}}}'
    )
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setattr(main, "Database", FakeDatabase)
    monkeypatch.setattr("main.OpenAIResponsesAdapter.aclose", fail_close)
    monkeypatch.setattr(main, "build_mcp_router", lambda _service: APIRouter())
    monkeypatch.setattr(main, "build_tools_router", lambda _service: APIRouter())
    with TestClient(create_app(tool_manager=cast(ToolManager, object()))) as client:
        assert client.get("/health").status_code == 200
    assert disposed == [True]


def test_os_error_outside_database_routes_is_not_masked_as_503() -> None:
    """未来外部 I/O 路由的 OSError 不能被数据库异常处理器全局吞掉。"""
    app: FastAPI = create_app()

    async def fail_external_io() -> None:
        raise ConnectionRefusedError("external service unavailable")

    app.add_api_route("/health/external-io", fail_external_io, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/external-io")

    assert response.status_code == 500
    assert response.status_code != 503
