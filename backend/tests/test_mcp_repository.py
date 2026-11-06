"""MCP Server 和工具快照 SQLite 原子仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.models import Base
from domain.auth import AuthContext, AuthSession, User, UserRole, UserStatus
from domain.mcp import (
    McpAuthorizationChangedError,
    McpConfigVersionConflictError,
    McpSecretError,
    McpServerNameConflictError,
    McpServerNotFoundError,
    McpServerWrite,
    StdioConfig,
)
from domain.tool import ToolDefinition
from infrastructure.security.mcp_secrets import McpSecretCipher

_NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def _tool(
    server_id: str, remote_name: str, tool_id: str, description: str = "Description"
) -> ToolDefinition:
    del tool_id
    tool_uuid = uuid4().hex
    return ToolDefinition(
        tool_id=f"tool_{tool_uuid}",
        server_id=server_id,
        remote_name=remote_name,
        model_name=f"mcp__{tool_uuid}",
        description=description,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


async def _setup() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyMcpRepository,
    AuthContext,
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    event.listen(
        engine.sync_engine,
        "connect",
        lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user = User(
        id="usr_mcp_admin",
        email="admin@example.com",
        password_hash="hash-not-used",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        role=UserRole.ADMIN,
    )
    auth_session = AuthSession(
        id="ses_mcp_admin",
        user_id=user.id,
        token_hash="1" * 64,
        expires_at=_NOW + timedelta(days=1),
        revoked_time=None,
        created_at=_NOW,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                UserModel(
                    id=user.id,
                    email=user.email,
                    password_hash=user.password_hash,
                    display_name=user.display_name,
                    status=user.status.value,
                    role=user.role.value,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                )
            )
            await session.flush()
            session.add(
                AuthSessionModel(
                    id=auth_session.id,
                    user_id=auth_session.user_id,
                    token_hash=auth_session.token_hash,
                    expires_at=auth_session.expires_at,
                    revoked_time=None,
                    created_at=auth_session.created_at,
                )
            )
    actor = AuthContext(user=user, session_id=auth_session.id)
    repository = SqlAlchemyMcpRepository(
        session_factory,
        McpSecretCipher(bytes(range(32)), key_version=1),
        clock=lambda: _NOW,
    )
    return engine, session_factory, repository, actor


def test_repository_atomically_creates_replaces_and_deletes_server_tools() -> None:
    async def scenario() -> None:
        engine, session_factory, repository, actor = await _setup()
        try:
            initial = McpServerWrite(
                id="mcp_1",
                name="Initial",
                config=StdioConfig("stdio", "/opt/mcp/server", ("--safe",), "/opt/mcp"),
                secrets={"API_TOKEN": "secret-value-one"},
            )
            created = await repository.create_with_tools(
                actor,
                initial,
                (_tool("mcp_1", "alpha", "tool_alpha"), _tool("mcp_1", "gamma", "tool_gamma")),
            )
            assert created.config_version == 1
            assert created.created_by == actor.user.id
            assert created.secret_metadata[0].name == "API_TOKEN"
            assert created.secret_metadata[0].has_value is True
            assert not hasattr(created, "secrets")
            stored_row = await repository.get("mcp_1")
            assert stored_row == created

            admin_tools = await repository.list_admin(offset=0, limit=10)
            before_alpha = next(tool for tool in admin_tools.items if tool.remote_name == "alpha")
            assert not hasattr(before_alpha, "config")
            assert not hasattr(before_alpha, "secrets")
            assert before_alpha.model_name.startswith("mcp__")
            public_tools = await repository.list_public(offset=0, limit=10)
            assert not hasattr(public_tools.items[0], "server_id")
            assert not hasattr(public_tools.items[0], "remote_name")

            async with session_factory() as session:
                async with session.begin():
                    stored_secret = await session.scalar(
                        select(McpServerModel.secrets_ciphertext).where(
                            McpServerModel.id == "mcp_1"
                        )
                    )
            assert stored_secret is not None
            assert b"secret-value-one" not in stored_secret
            assert b"API_TOKEN" not in stored_secret
            runtime = await repository.get_runtime_config("mcp_1")
            assert runtime is not None
            assert runtime.secrets["API_TOKEN"] == "secret-value-one"
            assert "secret-value-one" not in repr(runtime)

            replacement = McpServerWrite(
                id="mcp_1",
                name="Renamed",
                config=StdioConfig("stdio", "/opt/mcp/new-server"),
                secrets={"API_TOKEN": "secret-value-two"},
            )
            updated = await repository.replace_with_tools(
                actor,
                "mcp_1",
                1,
                replacement,
                (
                    _tool("mcp_1", "alpha", "ignored-new-alpha-id", "Changed"),
                    _tool("mcp_1", "beta", "tool_beta"),
                ),
            )
            assert updated.config_version == 2
            assert updated.name == "Renamed"
            after_tools = await repository.list_admin(offset=0, limit=10)
            after_alpha = next(tool for tool in after_tools.items if tool.remote_name == "alpha")
            after_beta = next(tool for tool in after_tools.items if tool.remote_name == "beta")
            assert (after_alpha.tool_id, after_alpha.model_name) == (
                before_alpha.tool_id,
                before_alpha.model_name,
            )
            assert after_alpha.description == "Changed"
            assert after_beta.tool_id.startswith("tool_")
            assert after_beta.model_name == f"mcp__{after_beta.tool_id.removeprefix('tool_')}"
            assert all(tool.remote_name != "gamma" for tool in after_tools.items)

            with pytest.raises(McpConfigVersionConflictError, match="mcp_config_version_conflict"):
                await repository.replace_with_tools(actor, "mcp_1", 1, initial, ())
            assert (await repository.get("mcp_1")).config_version == 2  # type: ignore[union-attr]
            assert (await repository.list_admin(offset=0, limit=10)).total == 2

            await repository.delete(actor, "mcp_1")
            assert await repository.get("mcp_1") is None
            assert (await repository.list_public(offset=0, limit=10)).total == 0
            with pytest.raises(McpServerNotFoundError):
                await repository.delete(actor, "mcp_1")
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_name_conflict_and_changed_auth_leave_no_partial_rows() -> None:
    async def scenario() -> None:
        engine, session_factory, repository, actor = await _setup()
        try:
            first = McpServerWrite(
                "mcp_first",
                "Shared Name",
                StdioConfig("stdio", "/opt/mcp/first"),
                {},
            )
            await repository.create_with_tools(
                actor, first, (_tool("mcp_first", "first", "tool_first"),)
            )
            duplicate = McpServerWrite(
                "mcp_duplicate",
                "Shared Name",
                StdioConfig("stdio", "/opt/mcp/duplicate"),
                {},
            )
            with pytest.raises(McpServerNameConflictError, match="mcp_server_name_conflict"):
                await repository.create_with_tools(
                    actor,
                    duplicate,
                    (_tool("mcp_duplicate", "must-rollback", "tool_rollback"),),
                )
            assert await repository.get("mcp_duplicate") is None

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("UPDATE auth_sessions SET revoked_time = :now WHERE id = :id"),
                        {"now": _NOW, "id": actor.session_id},
                    )
            denied = McpServerWrite(
                "mcp_denied",
                "Denied",
                StdioConfig("stdio", "/opt/mcp/denied"),
                {},
            )
            with pytest.raises(McpAuthorizationChangedError, match="mcp_authorization_changed"):
                await repository.create_with_tools(
                    actor,
                    denied,
                    (_tool("mcp_denied", "no-write", "tool_no_write"),),
                )
            assert await repository.get("mcp_denied") is None
            async with session_factory() as session:
                server_count = int(
                    await session.scalar(select(func.count()).select_from(McpServerModel)) or 0
                )
                tool_count = int(
                    await session.scalar(select(func.count()).select_from(McpToolModel)) or 0
                )
            assert server_count == 1
            assert tool_count == 1
        finally:
            await engine.dispose()

    _run(scenario())


def test_corrupt_secret_ciphertext_fails_get_list_and_update_closed() -> None:
    async def scenario() -> None:
        engine, session_factory, repository, actor = await _setup()
        try:
            initial = McpServerWrite(
                "mcp_corrupt",
                "Corruptible",
                StdioConfig("stdio", "/opt/mcp/server"),
                {"SECRET_ENV": "secret-value"},
            )
            await repository.create_with_tools(actor, initial, ())
            replacement = McpServerWrite(
                initial.id,
                "Replacement",
                StdioConfig("stdio", "/opt/mcp/new"),
                {},
            )
            async with session_factory() as session:
                row = await session.get(McpServerModel, initial.id)
                assert row is not None and row.secrets_ciphertext is not None
                original_ciphertext = row.secrets_ciphertext
                original_version = row.key_version
            corruptions = (
                (bytes([original_ciphertext[0] ^ 1]) + original_ciphertext[1:], original_version),
                (original_ciphertext[:-2], original_version),
                (original_ciphertext, (original_version or 0) + 1),
            )
            for ciphertext, key_version in corruptions:
                async with session_factory() as session:
                    async with session.begin():
                        row = await session.get(McpServerModel, initial.id)
                        assert row is not None
                        row.secrets_ciphertext = ciphertext
                        row.key_version = key_version
                with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
                    await repository.get(initial.id)
                with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
                    await repository.list_page(offset=0, limit=10)
                with pytest.raises(McpSecretError, match="mcp_secret_decrypt_failed"):
                    await repository.replace_with_tools(actor, initial.id, 1, replacement, ())
        finally:
            await engine.dispose()

    _run(scenario())
