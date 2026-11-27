"""验证 MCP API DTO 的联合判别与登录/管理员路由预检查。"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import ANY, AsyncMock

import anyio
import pytest
from auth_support import (
    TEST_PASSWORD,
    InMemoryAuthRepository,
    make_auth_service,
    provision_local_user_sync,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError  # type: ignore[import-untyped]
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.dto.mcp import (
    HttpServerCreateRequest,
    HttpServerUpdateRequest,
    McpServerCreateRequest,
    McpServerUpdateRequest,
    StdioServerCreateRequest,
    StdioServerUpdateRequest,
)
from api.errors import domain_error_to_http
from api.security import get_current_auth
from app.tool import ToolManager
from bootstrap.config import env_csv_set
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.mcp import (
    McpCatalogPersistenceError,
    McpConfigVersionConflictError,
    McpDiscoveryError,
    McpDiscoveryTimeoutError,
    McpSecretMetadata,
    McpServer,
    McpServerNotFoundError,
    McpServerPage,
    StreamableHttpConfig,
)
from domain.tool import AdminToolDefinition, PublicToolDefinition, ToolMetadataPatch, ToolPage
from main import create_app


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "x",
            "transport": "streamable_http",
            "url": "https://example.test",
            "command": None,
        },
        {"name": "x", "transport": "stdio", "command": "/bin/true", "url": None},
        {"name": "x", "transport": "streamable_http", "url": "https://example.test", "args": None},
        {"name": "x", "transport": "stdio", "command": "/bin/true", "extra": "x"},
    ],
)
def test_server_request_rejects_mixed_or_extra_fields(payload: dict[str, object]) -> None:
    adapter: TypeAdapter[HttpServerCreateRequest | StdioServerCreateRequest] = TypeAdapter(
        cast(Any, McpServerCreateRequest)
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


def test_server_update_requires_config_version() -> None:
    adapter: TypeAdapter[HttpServerUpdateRequest | StdioServerUpdateRequest] = TypeAdapter(
        cast(Any, McpServerUpdateRequest)
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"name": "x", "transport": "streamable_http", "url": "https://example.test"}
        )


@pytest.mark.parametrize(
    "request_type",
    [McpServerCreateRequest, McpServerUpdateRequest],
)
@pytest.mark.parametrize("args", ["missing", None])
def test_stdio_request_requires_explicit_args_array(request_type: object, args: object) -> None:
    payload: dict[str, object] = {
        "name": "x",
        "transport": "stdio",
        "command": "/bin/true",
        "secrets": {},
    }
    if request_type is McpServerUpdateRequest:
        payload["expected_config_version"] = 1
    if args != "missing":
        payload["args"] = args
    with pytest.raises(ValidationError):
        TypeAdapter(cast(Any, request_type)).validate_python(payload)


def test_stdio_empty_args_and_http_absent_args_are_valid() -> None:
    adapter: TypeAdapter[HttpServerCreateRequest | StdioServerCreateRequest] = TypeAdapter(
        cast(Any, McpServerCreateRequest)
    )
    stdio = adapter.validate_python(
        {"name": "x", "transport": "stdio", "command": "/bin/true", "args": []}
    )
    http = adapter.validate_python(
        {"name": "x", "transport": "streamable_http", "url": "https://example.test"}
    )
    assert isinstance(stdio, StdioServerCreateRequest)
    assert stdio.args == []
    assert http.transport == "streamable_http"


def test_update_requires_secrets_but_empty_map_is_valid() -> None:
    adapter: TypeAdapter[HttpServerUpdateRequest | StdioServerUpdateRequest] = TypeAdapter(
        cast(Any, McpServerUpdateRequest)
    )
    base = {
        "expected_config_version": 1,
        "name": "x",
        "transport": "streamable_http",
        "url": "https://example.test",
    }
    with pytest.raises(ValidationError):
        adapter.validate_python(base)
    request = adapter.validate_python({**base, "secrets": {}})
    assert request.secrets == {}


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (McpConfigVersionConflictError("sentinel-secret"), 409, "mcp_conflict"),
        (McpDiscoveryError("sentinel-secret"), 502, "mcp_discovery_failed"),
        (McpDiscoveryTimeoutError("sentinel-secret"), 504, "mcp_discovery_timeout"),
        (McpCatalogPersistenceError("sentinel-secret"), 503, "mcp_unavailable"),
    ],
)
def test_mcp_error_mapping_uses_only_fixed_codes(
    error: Exception, expected_status: int, expected_detail: str
) -> None:
    response = domain_error_to_http(error)
    assert response is not None
    assert response.status_code == expected_status
    assert response.detail == expected_detail
    assert "sentinel-secret" not in str(response.detail)


class _PublicOnlyManager:
    def __init__(self, tool: PublicToolDefinition | None = None) -> None:
        self._tool = tool

    async def list_public_tools(
        self, actor: object, *, offset: int, limit: int
    ) -> ToolPage[object]:
        del actor, offset, limit
        return ToolPage(
            items=() if self._tool is None else (self._tool,), total=int(self._tool is not None)
        )

    async def get_public_tool(self, actor: object, tool_id: str) -> PublicToolDefinition:
        del actor
        if self._tool is None or self._tool.tool_id != tool_id:
            raise McpServerNotFoundError("sentinel")
        return self._tool


def test_admin_routes_return_redacted_server_and_explicit_tool_projections() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    server = McpServer(
        id="mcp_test",
        name="Test MCP",
        config=StreamableHttpConfig(transport="streamable_http", url="https://admin.example/mcp"),
        secret_metadata=(McpSecretMetadata(name="Authorization", has_value=True),),
        key_version=3,
        config_version=1,
        created_by="user_1",
        created_at=now,
        updated_at=now,
        tools_discovered_at=now,
    )
    tool = AdminToolDefinition(
        tool_id="tool_0123456789abcdef0123456789abcdef",
        model_name="mcp__0123456789abcdef0123456789abcdef",
        description="safe description",
        input_schema={"type": "object"},
        server_id=server.id,
        remote_name="remote_tool",
    )
    manager = _PublicOnlyManager()
    manager.register_mcp = AsyncMock(return_value=server)  # type: ignore[attr-defined]
    manager.list_mcp_servers = AsyncMock(return_value=McpServerPage((server,), 1))  # type: ignore[attr-defined]
    manager.get_mcp_server = AsyncMock(return_value=server)  # type: ignore[attr-defined]
    manager.update_mcp = AsyncMock(return_value=server)  # type: ignore[attr-defined]
    manager.unregister_mcp = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    manager.list_server_tools = AsyncMock(return_value=ToolPage((tool,), 1))  # type: ignore[attr-defined]
    manager.list_admin_tools = AsyncMock(return_value=ToolPage((tool,), 1))  # type: ignore[attr-defined]
    manager.get_admin_tool = AsyncMock(return_value=tool)  # type: ignore[attr-defined]
    manager.update_tool_metadata = AsyncMock(  # type: ignore[attr-defined]
        return_value=replace(tool, requires_approval=True)
    )
    auth_service = make_auth_service()
    repository = cast(InMemoryAuthRepository, auth_service._repository)
    provision_local_user_sync(auth_service, email="mcp-admin@example.com", password=TEST_PASSWORD)
    app = create_app(auth_service=auth_service, tool_manager=cast(ToolManager, manager))

    with TestClient(app) as client:
        logged_in = client.post(
            "/api/v1/auth/login",
            json={"email": "mcp-admin@example.com", "password": TEST_PASSWORD},
        )
        assert logged_in.status_code == 200
        anyio.run(repository.promote_active_user_by_email, "mcp-admin@example.com", now)
        created = client.post(
            "/api/v1/admin/mcp/servers",
            json={
                "name": "Test MCP",
                "transport": "streamable_http",
                "url": "https://request.example/mcp",
                "secrets": {"Authorization": "secret-sentinel"},
            },
        )
        listed = client.get("/api/v1/admin/mcp/servers")
        detail = client.get("/api/v1/admin/mcp/servers/mcp_test")
        updated = client.put(
            "/api/v1/admin/mcp/servers/mcp_test",
            json={
                "expected_config_version": 1,
                "name": "Test MCP",
                "transport": "streamable_http",
                "url": "https://request.example/mcp",
                "secrets": {},
            },
        )
        missing_secrets_update = client.put(
            "/api/v1/admin/mcp/servers/mcp_test",
            json={
                "expected_config_version": 1,
                "name": "Test MCP",
                "transport": "streamable_http",
                "url": "https://request.example/mcp",
            },
        )
        server_tools = client.get("/api/v1/admin/mcp/servers/mcp_test/tools")
        admin_tools = client.get("/api/v1/admin/tools")
        admin_tool = client.get(f"/api/v1/admin/tools/{tool.tool_id}")
        denied_policy = client.patch(
            f"/api/v1/admin/tools/{tool.tool_id}",
            headers={"Origin": "https://evil.example.com"},
            json={"requires_approval": True},
        )
        invalid_policy = client.patch(
            f"/api/v1/admin/tools/{tool.tool_id}",
            json={"requires_approval": True, "server_id": "must-not-change"},
        )
        updated_policy = client.patch(
            f"/api/v1/admin/tools/{tool.tool_id}",
            json={"requires_approval": True},
        )
        deleted = client.delete("/api/v1/admin/mcp/servers/mcp_test")
        public_tools = client.get("/api/v1/tools")

    assert created.status_code == 201
    assert created.headers["location"].endswith("/mcp_test")
    assert created.headers["cache-control"] == "no-store"
    assert listed.json()["total"] == 1
    assert detail.json()["config"] == {"url": "https://admin.example/mcp"}
    assert detail.json()["secrets"] == [{"name": "Authorization", "has_value": True}]
    assert "secret-sentinel" not in created.text + detail.text + updated.text
    assert updated.status_code == 200
    assert missing_secrets_update.status_code == 422
    assert missing_secrets_update.json() == {"detail": "mcp_request_invalid"}
    assert server_tools.json()["items"][0]["remote_name"] == "remote_tool"
    assert admin_tools.json()["total"] == 1
    assert admin_tool.json()["server_id"] == "mcp_test"
    assert denied_policy.status_code == 403
    assert invalid_policy.status_code == 422
    assert updated_policy.status_code == 200
    assert updated_policy.json()["requires_approval"] is True
    manager.update_tool_metadata.assert_awaited_once_with(  # type: ignore[attr-defined]
        ANY, tool.tool_id, ToolMetadataPatch(requires_approval=True)
    )
    assert deleted.status_code == 204
    assert public_tools.status_code == 200
    assert "server_id" not in public_tools.text and "remote_name" not in public_tools.text


def test_regular_user_cannot_update_tool_approval_policy() -> None:
    """普通用户即使提交合法 DTO 也不能修改管理员工具策略。"""

    auth_service = make_auth_service()
    provision_local_user_sync(
        auth_service, email="regular-policy@example.com", password=TEST_PASSWORD
    )
    app = create_app(
        auth_service=auth_service,
        tool_manager=cast(ToolManager, _PublicOnlyManager()),
    )
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/v1/auth/login",
            json={"email": "regular-policy@example.com", "password": TEST_PASSWORD},
        )
        assert logged_in.status_code == 200
        denied = client.patch(
            "/api/v1/admin/tools/tool_0123456789abcdef0123456789abcdef",
            json={"requires_approval": True},
        )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "admin_required"}


def test_mcp_routes_require_login_and_admin_precheck() -> None:
    public_tool = PublicToolDefinition(
        tool_id="tool_0123456789abcdef0123456789abcdef",
        model_name="mcp__0123456789abcdef0123456789abcdef",
        description="public description",
        input_schema={"type": "object"},
    )
    auth_service = make_auth_service()
    provision_local_user_sync(auth_service, email="api-user@example.com", password=TEST_PASSWORD)
    app = create_app(
        auth_service=auth_service,
        tool_manager=cast(ToolManager, _PublicOnlyManager(public_tool)),
    )
    with TestClient(app) as client:
        anonymous_admin = client.get("/api/v1/admin/mcp/servers")
        anonymous_tools = client.get("/api/v1/tools")
        anonymous_expanded_query = client.get("/api/v1/tools?include_source=true")
        anonymous_invalid_json = client.post(
            "/api/v1/admin/mcp/servers",
            content="{invalid-json",
            headers={"content-type": "application/json"},
        )
        registered = client.post(
            "/api/v1/auth/login",
            json={"email": "api-user@example.com", "password": TEST_PASSWORD},
        )
        user_invalid_json = client.post(
            "/api/v1/admin/mcp/servers",
            content="{invalid-json",
            headers={"content-type": "application/json"},
        )
        user_expanded_query = client.get("/api/v1/tools?include_source=true")
        user_repeated_query = client.get("/api/v1/tools?page=1&page=2")
        user_valid_pagination = client.get("/api/v1/tools?page=1&page_size=1")
        app.dependency_overrides[get_current_auth] = lambda: AuthContext(
            user=User(
                id="admin-test",
                email="admin@example.com",
                password_hash="unused",
                display_name=None,
                status=UserStatus.ACTIVE,
                role=UserRole.SUPER_ADMIN,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            session_id="session-test",
        )
        rejected_config = client.post(
            "/api/v1/admin/mcp/servers",
            json={
                "name": "private",
                "transport": "streamable_http",
                "url": "https://sentinel-host.example/secret-path",
                "command": None,
                "secrets": {"Authorization": "sentinel-secret-value"},
            },
        )
        admin_invalid_json = client.post(
            "/api/v1/admin/mcp/servers",
            content="{invalid-json",
            headers={"content-type": "application/json"},
        )
        app.dependency_overrides.clear()
        regular_admin = client.get("/api/v1/admin/mcp/servers/missing")
        regular_admin_tools = client.get("/api/v1/admin/tools")
        public_tools = client.get("/api/v1/tools")
        public_detail_with_query = client.get(
            f"/api/v1/tools/{public_tool.tool_id}?include_source=true"
        )
        public_detail = client.get(f"/api/v1/tools/{public_tool.tool_id}")
        missing_public_detail = client.get("/api/v1/tools/missing")

    assert anonymous_admin.status_code == 401
    assert anonymous_admin.json() == {"detail": "not_authenticated"}
    assert anonymous_tools.status_code == 401
    assert anonymous_expanded_query.status_code == 401
    assert anonymous_invalid_json.status_code == 401
    assert registered.status_code == 200
    assert user_invalid_json.status_code == 403
    assert user_expanded_query.status_code == user_repeated_query.status_code == 422
    assert (
        user_expanded_query.json() == user_repeated_query.json() == {"detail": "mcp_query_invalid"}
    )
    assert user_valid_pagination.status_code == 200
    assert admin_invalid_json.status_code == 422
    assert admin_invalid_json.json() == {"detail": "mcp_request_invalid"}
    assert rejected_config.status_code == 422
    assert rejected_config.json() == {"detail": "mcp_request_invalid"}
    assert "sentinel-secret-value" not in rejected_config.text
    assert "sentinel-host.example" not in rejected_config.text
    assert regular_admin.status_code == regular_admin_tools.status_code == 403
    assert regular_admin.json() == regular_admin_tools.json() == {"detail": "admin_required"}
    assert public_tools.status_code == 200
    public_item = public_tools.json()["items"][0]
    assert set(public_item) == {"tool_id", "model_name", "description", "input_schema"}
    assert "server_id" not in public_item and "remote_name" not in public_item
    assert set(public_detail.json()) == {"tool_id", "model_name", "description", "input_schema"}
    assert public_detail_with_query.status_code == 422
    assert public_detail_with_query.json() == {"detail": "mcp_query_invalid"}
    assert missing_public_detail.status_code == 404


def test_injected_tool_manager_mounts_mcp_routes_without_database() -> None:
    app = create_app(tool_manager=cast(ToolManager, _PublicOnlyManager()))
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/mcp/servers").status_code == 401
        assert client.get("/api/v1/tools").status_code == 401


def test_admin_write_openapi_declares_valid_transport_request_bodies() -> None:
    app = create_app(tool_manager=cast(ToolManager, _PublicOnlyManager()))
    openapi_doc = app.openapi()
    paths = openapi_doc["paths"]
    component_schemas = openapi_doc["components"]["schemas"]
    post_body = paths["/api/v1/admin/mcp/servers"]["post"]["requestBody"]
    put_body = paths["/api/v1/admin/mcp/servers/{server_id}"]["put"]["requestBody"]
    assert post_body["required"] is True
    assert put_body["required"] is True
    assert set(post_body["content"]) == {"application/json"}
    assert set(put_body["content"]) == {"application/json"}
    create_schema = post_body["content"]["application/json"]["schema"]
    update_schema = put_body["content"]["application/json"]["schema"]
    for schema in (create_schema, update_schema):
        assert "$defs" not in schema
        resolved_schema = {**schema, "components": openapi_doc["components"]}
        Draft202012Validator.check_schema(resolved_schema)
        validator = Draft202012Validator(resolved_schema)
        assert schema["discriminator"]["propertyName"] == "transport"
        assert len(schema["oneOf"]) == 2
        stdio_pointer = schema["discriminator"]["mapping"]["stdio"]
        http_pointer = schema["discriminator"]["mapping"]["streamable_http"]
        assert stdio_pointer.startswith("#/components/schemas/")
        assert http_pointer.startswith("#/components/schemas/")
        stdio_ref = stdio_pointer.rsplit("/", 1)[-1]
        http_ref = http_pointer.rsplit("/", 1)[-1]
        stdio_rule = component_schemas[stdio_ref]
        http_rule = component_schemas[http_ref]
        assert {"name", "transport", "command", "args"} <= set(stdio_rule["required"])
        assert stdio_rule["properties"]["command"] == {"title": "Command", "type": "string"}
        assert stdio_rule["properties"]["args"] == {
            "items": {"type": "string"},
            "title": "Args",
            "type": "array",
        }
        assert http_rule["properties"]["url"] == {"title": "Url", "type": "string"}
        http_valid: dict[str, object] = {
            "name": "server",
            "transport": "streamable_http",
            "url": "https://example.test",
        }
        stdio_valid: dict[str, object] = {
            "name": "server",
            "transport": "stdio",
            "command": "/bin/true",
            "args": [],
        }
        if schema is update_schema:
            http_valid.update(expected_config_version=1, secrets={})
            stdio_valid.update(expected_config_version=1, secrets={})
        validator.validate(http_valid)
        validator.validate(stdio_valid)
        with pytest.raises(JsonSchemaValidationError):
            validator.validate({**http_valid, "url": None})
        with pytest.raises(JsonSchemaValidationError):
            validator.validate({**stdio_valid, "command": None})


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("sentinel https://secret.example /usr/local/bin/server"),
        SQLAlchemyError("sentinel https://secret.example /usr/local/bin/server"),
    ],
)
def test_database_exception_handlers_cover_mcp_paths(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    app: FastAPI = create_app()

    async def fail() -> None:
        raise error

    app.add_api_route("/api/v1/admin/failure", fail, methods=["GET"])
    with caplog.at_level("ERROR"), TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/admin/failure")
    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    for sentinel in ("sentinel", "secret.example", "/usr/local/bin/server"):
        assert sentinel not in response.text
        assert sentinel not in caplog.text


def test_database_default_mcp_assembly_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.mcp import McpSecretError

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://db.invalid/athena")
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.internal:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "ak")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "sk")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.delenv("ATHENA_MCP_SECRET_KEY", raising=False)
    with pytest.raises(McpSecretError, match="mcp_secret_key_missing"):
        create_app()


def test_database_default_assembly_reuses_auth_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    import minio

    class _FakeMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def bucket_exists(self, _bucket: str) -> bool:
            return True

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://db.invalid/athena")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.internal:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "ak")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "sk")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.delenv("ATHENA_MCP_STDIO_BASE_ENV_NAMES", raising=False)
    monkeypatch.setattr(minio, "Minio", _FakeMinio)
    app = create_app()
    manager = app.state.tool_manager
    authenticator = app.state.authenticator
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert authenticator._repository is manager._auth
    assert manager._servers is manager._tools


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ATHENA_MCP_STDIO_BASE_ENV_NAMES", "GOOD_NAME,,OTHER_NAME"),
        ("ATHENA_MCP_STDIO_BASE_ENV_NAMES", "NOT-AN-ENV"),
    ],
)
def test_deployment_allowlist_rejects_empty_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"{name}_invalid"):
        env_csv_set(name)
