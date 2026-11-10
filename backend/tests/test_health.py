"""验证应用组装后的 HTTP 存活检查。"""

from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import bootstrap.api as bootstrap_api
import bootstrap.database as bootstrap_database
import main
from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.tool import ToolManager
from bootstrap.tool import QueryServiceBundle, build_query_services
from domain.knowledge.model import OriginalStorageConfigurationError
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


def test_disabled_knowledge_does_not_parse_minio_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "false")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_MINIO_ENDPOINT", "https://invalid")
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_BUCKET", raising=False)
    bundle = build_query_services(
        cast(Any, object()),
        projects=cast(ProjectService, object()),
    )
    assert bundle.knowledge is None
    assert bundle.knowledge_original_store is None


def test_enabled_knowledge_requires_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_DATABASE_URL", raising=False)
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")

    with pytest.raises(RuntimeError, match="knowledge_database_required"):
        create_app()


@pytest.mark.parametrize(
    "overrides",
    (
        {},
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "localhost:abc",
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
        },
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "localhost:9000",
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "Invalid_Bucket",
        },
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "localhost:9000",
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
            "ATHENA_KNOWLEDGE_MINIO_SECURE": "sometimes",
        },
    ),
)
def test_knowledge_minio_configuration_fails_before_creating_query_resources(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    created: list[str] = []

    class FakeDatabase:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            created.append("database")

    def unexpected_query_services(*_args: Any, **_kwargs: Any) -> QueryServiceBundle:
        created.append("query_services")
        raise AssertionError("query services must not be built")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
    for name in (
        "ATHENA_KNOWLEDGE_MINIO_ENDPOINT",
        "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY",
        "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY",
        "ATHENA_KNOWLEDGE_MINIO_BUCKET",
        "ATHENA_KNOWLEDGE_MINIO_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_api, "build_query_services", unexpected_query_services)

    with pytest.raises(OriginalStorageConfigurationError):
        create_app(project_service=cast(ProjectService, object()))
    assert created == []


def test_query_service_start_failure_closes_started_resources_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeDatabase:
        session_factory = object()

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def dispose(self) -> None:
            events.append("database")

    class FailingResource:
        async def start(self) -> None:
            events.append("start")
            raise RuntimeError("storage unavailable")

        async def aclose(self) -> None:
            events.append("close")

    resource = FailingResource()
    bundle = QueryServiceBundle(
        memory=None,
        knowledge=None,
        project_knowledge=None,
        knowledge_repository=None,
        knowledge_original_store=None,
        memory_enabled=False,
        knowledge_enabled=False,
        _startables=(resource,),
        _closables=(resource,),
    )
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_api, "build_query_services", lambda *_args, **_kwargs: bundle)

    app = create_app(
        project_service=cast(ProjectService, object()),
        tool_manager=cast(ToolManager, object()),
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        with TestClient(app):
            pass
    assert events == ["start", "close", "database"]


def test_injected_knowledge_service_mounts_project_routes_without_stealing_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库组装共享查询服务时挂载项目知识路由，但不关闭调用方资源。"""

    class FakeDatabase:
        session_factory = object()

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.disposed = False
            databases.append(self)

        async def dispose(self) -> None:
            self.disposed = True

    databases: list[FakeDatabase] = []

    class InjectedKnowledge:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    captured: list[QueryServiceBundle] = []

    def capture_query_services(*args: Any, **kwargs: Any) -> QueryServiceBundle:
        bundle = build_query_services(*args, **kwargs)
        captured.append(bundle)
        return bundle

    knowledge = InjectedKnowledge()
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_MINIO_ENDPOINT", "localhost:not-a-port")
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_KNOWLEDGE_MINIO_BUCKET", raising=False)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr("bootstrap.api.build_query_services", capture_query_services)
    app = create_app(
        project_service=cast(ProjectService, object()),
        tool_manager=cast(ToolManager, object()),
        knowledge_service=cast(KnowledgeService, knowledge),
    )

    assert captured
    assert isinstance(captured[0].project_knowledge, ProjectKnowledgeService)
    assert "/api/v1/projects/{project_id}/knowledge-bases" in app.openapi()["paths"]
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert not knowledge.closed
    assert len(databases) == 1
    assert databases[0].disposed


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
    monkeypatch.setattr("bootstrap.api.OpenAIResponsesAdapter.aclose", close)
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
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr("bootstrap.api.OpenAIResponsesAdapter.aclose", fail_close)
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
