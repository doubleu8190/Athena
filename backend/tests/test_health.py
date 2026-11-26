"""验证应用组装后的 HTTP 存活检查。"""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import bootstrap.api as bootstrap_api
import bootstrap.database as bootstrap_database
import main
from adapter.mcp.repository import SqlAlchemyMcpRepository
from app.knowledge.service import KnowledgeService
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.tool import ToolManager
from bootstrap.tool import QueryServiceBundle, build_query_services
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.knowledge.model import OriginalStorageConfigurationError
from domain.mcp import McpServer, McpServerPage, StreamableHttpConfig
from domain.preference import PreferenceRepository
from domain.tool import AdminToolDefinition, ToolPage
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


def test_request_validation_errors_do_not_echo_sensitive_input() -> None:
    """请求校验错误响应不回显管理员提交的 API Key。"""
    request = Request({"type": "http", "method": "POST", "path": "/"})
    error = RequestValidationError(
        [
            {
                "type": "string_type",
                "loc": ("body", "api_key"),
                "msg": "Input should be a valid string",
                "input": "sensitive-key-value",
            }
        ]
    )
    response = asyncio.run(main._handle_request_validation_error(request, error))
    assert response.status_code == 422
    assert b"sensitive-key-value" not in response.body


def test_preference_service_is_none_without_database_and_can_be_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_DATABASE_URL", raising=False)
    bare_app = create_app()
    assert bare_app.state.preference_service is None

    service = PreferenceService(cast(PreferenceRepository, object()))
    injected_app = create_app(preference_service=service)
    assert injected_app.state.preference_service is service
    assert "/api/v1/me/preferences" in injected_app.openapi()["paths"]
    assert "/api/v1/me/memories" in injected_app.openapi()["paths"]


def test_lightweight_mode_without_database_skips_storage_and_embedding_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 ATHENA_DATABASE_URL 时保持轻量模式，支持纯 /health 探活，不要求对象存储或 Embedding。"""
    monkeypatch.delenv("ATHENA_DATABASE_URL", raising=False)
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "https://invalid")
    monkeypatch.delenv("ATHENA_STORAGE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_BUCKET", raising=False)
    monkeypatch.delenv("ATHENA_EMBEDDING_BASE_URL", raising=False)

    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_file_storage_configuration_fails_before_creating_database_when_knowledge_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入 knowledge_service 但自建文件/技能服务时，非法存储配置须在建库前 Fail-Fast 拒绝。"""
    from domain.file import FileStorageError

    created: list[str] = []

    class FakeDatabase:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            created.append("database")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.delenv("ATHENA_STORAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_BUCKET", raising=False)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)

    with pytest.raises(FileStorageError, match="file_storage_configuration_invalid"):
        create_app(
            project_service=cast(ProjectService, object()),
            tool_manager=cast(ToolManager, object()),
            knowledge_service=cast(KnowledgeService, object()),
        )
    assert created == []


@pytest.mark.parametrize(
    "overrides",
    (
        {},
        {
            "ATHENA_STORAGE_ENDPOINT": "localhost:abc",
            "ATHENA_STORAGE_ACCESS_KEY": "access",
            "ATHENA_STORAGE_SECRET_KEY": "secret",
            "ATHENA_STORAGE_BUCKET": "test-bucket",
        },
        {
            "ATHENA_STORAGE_ENDPOINT": "localhost:9000",
            "ATHENA_STORAGE_ACCESS_KEY": "access",
            "ATHENA_STORAGE_SECRET_KEY": "secret",
            "ATHENA_STORAGE_BUCKET": "Invalid_Bucket",
        },
        {
            "ATHENA_STORAGE_ENDPOINT": "localhost:9000",
            "ATHENA_STORAGE_ACCESS_KEY": "access",
            "ATHENA_STORAGE_SECRET_KEY": "secret",
            "ATHENA_STORAGE_BUCKET": "test-bucket",
            "ATHENA_STORAGE_SECURE": "sometimes",
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
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    for name in (
        "ATHENA_STORAGE_ENDPOINT",
        "ATHENA_STORAGE_ACCESS_KEY",
        "ATHENA_STORAGE_SECRET_KEY",
        "ATHENA_STORAGE_BUCKET",
        "ATHENA_STORAGE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_api, "build_query_services", unexpected_query_services)

    with pytest.raises(OriginalStorageConfigurationError):
        create_app(project_service=cast(ProjectService, object()))
    assert created == []


def test_knowledge_embedding_configuration_fails_before_creating_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infrastructure.embedding import EmbeddingConfigurationError

    created: list[str] = []

    class FakeDatabase:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            created.append("database")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.delenv("ATHENA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)

    with pytest.raises(EmbeddingConfigurationError, match="embedding_base_url_missing"):
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
        knowledge=None,
        project_knowledge=None,
        knowledge_repository=None,
        knowledge_original_store=None,
        _startables=(resource,),
        _closables=(resource,),
    )
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_api, "build_query_services", lambda *_args, **_kwargs: bundle)

    app = create_app(
        project_service=cast(ProjectService, object()),
        tool_manager=cast(ToolManager, object()),
        skill_service=cast(Any, object()),
        session_file_service=cast(Any, object()),
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
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    # 知识库与文件/技能均由外部注入时，组合根不得解析对象存储或 Embedding 环境变量。
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "localhost:not-a-port")
    monkeypatch.delenv("ATHENA_STORAGE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATHENA_STORAGE_BUCKET", raising=False)
    monkeypatch.delenv("ATHENA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr("bootstrap.api.build_query_services", capture_query_services)
    app = create_app(
        project_service=cast(ProjectService, object()),
        tool_manager=cast(ToolManager, object()),
        knowledge_service=cast(KnowledgeService, knowledge),
        skill_service=cast(Any, object()),
        session_file_service=cast(Any, object()),
    )

    assert captured
    assert isinstance(captured[0].project_knowledge, ProjectKnowledgeService)
    assert "/api/v1/projects/{project_id}/knowledge-bases" in app.openapi()["paths"]
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert not knowledge.closed
    assert len(databases) == 1
    assert databases[0].disposed


def test_injected_tool_manager_keeps_mcp_tools_in_default_subagent_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入 ToolManager 时，默认子代理目录仍读取同库的 MCP 工具。"""
    now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    admin = User(
        id="usr_admin",
        email="admin@example.com",
        password_hash="unused",
        display_name="Admin",
        status=UserStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        role=UserRole.SUPER_ADMIN,
    )
    actor = AuthContext(user=admin, session_id="sess_admin")
    created_repositories: list[SqlAlchemyMcpRepository] = []

    class FakeDatabase:
        session_factory = object()

        async def dispose(self) -> None:
            return None

    class FakeMcpRepository:
        def __init__(self, session_factory: object, secret_cipher: object) -> None:
            del session_factory, secret_cipher
            created_repositories.append(cast(SqlAlchemyMcpRepository, self))

        async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
            del offset, limit
            return McpServerPage(
                items=(
                    McpServer(
                        id="mcp_github",
                        name="GitHub",
                        config=StreamableHttpConfig(
                            transport="streamable_http",
                            url="https://example.com/mcp",
                        ),
                        secret_metadata=(),
                        key_version=None,
                        config_version=1,
                        created_by=admin.id,
                        created_at=now,
                        updated_at=now,
                        tools_discovered_at=now,
                    ),
                ),
                total=1,
            )

        async def list_admin(
            self, *, offset: int, limit: int
        ) -> ToolPage[AdminToolDefinition]:
            del offset, limit
            return ToolPage(
                items=(
                    AdminToolDefinition(
                        tool_id="tool_" + "a" * 32,
                        model_name="mcp__github_search",
                        description="Search GitHub",
                        input_schema={"type": "object"},
                        server_id="mcp_github",
                        remote_name="search",
                    ),
                ),
                total=1,
            )

    class FakeAuthRepository:
        def __init__(self, session_factory: object) -> None:
            del session_factory

        async def get_current_session_user(
            self,
            user_id: str,
            session_id: str,
            current_time: datetime,
        ) -> User | None:
            del current_time
            assert (user_id, session_id) == (admin.id, actor.session_id)
            return admin

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_api, "build_database", lambda _url: FakeDatabase())
    monkeypatch.setattr(bootstrap_api, "SqlAlchemyMcpRepository", FakeMcpRepository)
    monkeypatch.setattr(bootstrap_api, "SqlAlchemyAuthRepository", FakeAuthRepository)

    dependencies = bootstrap_api.build_api_dependencies(
        project_service=cast(Any, object()),
        message_service=cast(Any, object()),
        session_service=cast(Any, object()),
        run_service=cast(Any, object()),
        auth_service=cast(Any, object()),
        tool_manager=cast(ToolManager, object()),
        model_profile_service=cast(Any, object()),
        system_prompt_service=cast(Any, object()),
    )

    assert len(created_repositories) == 1
    assert dependencies.subagent_service is not None
    groups = asyncio.run(dependencies.subagent_service.list_available_tools(actor))
    mcp_group = next(group for group in groups if group.group_key == "mcp")
    assert [(item.tool_id, item.name) for item in mcp_group.items] == [
        ("tool_" + "a" * 32, "mcp__github_search")
    ]
    asyncio.run(dependencies.aclose())


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
