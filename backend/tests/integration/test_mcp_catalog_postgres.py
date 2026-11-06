"""MCP 目录迁移、约束和原子写入 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.mcp.repository import SqlAlchemyMcpRepository
from domain.auth import AuthContext, AuthSession, User, UserRole, UserStatus
from domain.mcp import (
    McpAuthorizationChangedError,
    McpConfigVersionConflictError,
    McpServerNameConflictError,
    McpServerWrite,
    StdioConfig,
    StreamableHttpConfig,
)
from domain.tool import ToolDefinition
from infrastructure.security.mcp_secrets import McpSecretCipher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


def _tool(server_id: str, remote_name: str, tool_id: str) -> ToolDefinition:
    del tool_id
    tool_uuid = uuid4().hex
    return ToolDefinition(
        tool_id=f"tool_{tool_uuid}",
        server_id=server_id,
        remote_name=remote_name,
        model_name=f"mcp__{tool_uuid}",
        description="PostgreSQL integration tool",
        input_schema={"type": "object", "properties": {}},
    )


def test_mcp_migration_constraints_versions_and_auth_atomicity() -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        suffix = token_hex(8)
        schema = f"athena_v2_mcp_catalog_{suffix}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        test_engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": schema}
            }
            expected_head = ScriptDirectory.from_config(migration_config).get_current_head()
            await asyncio.to_thread(command.upgrade, migration_config, "head")
            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_head

            session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
            now = datetime.now(UTC)
            user = User(
                id=f"usr_mcp_{suffix}",
                email=f"{suffix}@example.com",
                password_hash="unused",
                display_name=None,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                role=UserRole.ADMIN,
            )
            auth_session = AuthSession(
                id=f"ses_mcp_{suffix}",
                user_id=user.id,
                token_hash=(suffix * 64)[:64],
                expires_at=now + timedelta(days=1),
                revoked_time=None,
                created_at=now,
            )
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id=user.id,
                            email=user.email,
                            password_hash=user.password_hash,
                            display_name=None,
                            status=user.status.value,
                            role=user.role.value,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                    session.add(
                        AuthSessionModel(
                            id=auth_session.id,
                            user_id=user.id,
                            token_hash=auth_session.token_hash,
                            expires_at=auth_session.expires_at,
                            revoked_time=None,
                            created_at=now,
                        )
                    )
            actor = AuthContext(user=user, session_id=auth_session.id)
            repository = SqlAlchemyMcpRepository(
                session_factory,
                McpSecretCipher(bytes(range(32)), 2),
                clock=lambda: now,
            )

            initial = McpServerWrite(
                id=f"mcp_{suffix}",
                name="PostgreSQL MCP",
                config=StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc"),
                secrets={"Authorization": "sensitive-value"},
            )
            created = await repository.create_with_tools(
                actor,
                initial,
                (_tool(initial.id, "alpha", f"tool_{suffix}_alpha"),),
            )
            assert created.key_version == 2
            assert created.secret_metadata[0].name == "Authorization"
            rows = await repository.list_admin(offset=0, limit=10)
            stable_id = rows.items[0].tool_id
            stable_model = rows.items[0].model_name

            duplicate = McpServerWrite(
                id=f"mcp_duplicate_{suffix}",
                name="PostgreSQL MCP",
                config=StdioConfig("stdio", "/opt/mcp/server"),
                secrets={},
            )
            with pytest.raises(McpServerNameConflictError):
                await repository.create_with_tools(
                    actor,
                    duplicate,
                    (_tool(duplicate.id, "rollback", f"tool_{suffix}_rollback"),),
                )
            assert await repository.get(duplicate.id) is None

            replacement = McpServerWrite(
                id=initial.id,
                name="PostgreSQL MCP updated",
                config=StdioConfig("stdio", "/opt/mcp/server"),
                secrets={},
            )
            updated = await repository.replace_with_tools(
                actor,
                initial.id,
                1,
                replacement,
                (_tool(initial.id, "alpha", f"tool_{suffix}_ignored"),),
            )
            assert updated.config_version == 2
            updated_tools = await repository.list_admin(offset=0, limit=10)
            assert updated_tools.items[0].tool_id == stable_id
            assert updated_tools.items[0].model_name == stable_model
            with pytest.raises(McpConfigVersionConflictError):
                await repository.replace_with_tools(actor, initial.id, 1, initial, ())
            assert (await repository.get(initial.id)).config_version == 2  # type: ignore[union-attr]

            server_table_id = initial.id
            with pytest.raises(IntegrityError):
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE mcp_servers SET transport = 'invalid' WHERE id = :id"),
                        {"id": server_table_id},
                    )
            with pytest.raises(IntegrityError):
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE mcp_servers SET config_version = 0 WHERE id = :id"),
                        {"id": server_table_id},
                    )
            with pytest.raises(IntegrityError):
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE mcp_servers SET secrets_nonce = :nonce WHERE id = :id"),
                        {"id": server_table_id, "nonce": b"partial"},
                    )

            await repository.delete(actor, initial.id)
            assert await repository.get(initial.id) is None
            assert (await repository.list_admin(offset=0, limit=10)).total == 0

            await repository.create_with_tools(
                actor,
                initial,
                (_tool(initial.id, "existing", f"tool_{suffix}_existing"),),
            )
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("UPDATE auth_sessions SET revoked_time = :now WHERE id = :id"),
                        {"now": now, "id": auth_session.id},
                    )
            denied = McpServerWrite(
                id=f"mcp_denied_{suffix}",
                name="Denied due to revoked auth",
                config=StdioConfig("stdio", "/opt/mcp/server"),
                secrets={},
            )
            with pytest.raises(McpAuthorizationChangedError):
                await repository.create_with_tools(
                    actor,
                    denied,
                    (_tool(denied.id, "must-not-commit", f"tool_{suffix}_denied"),),
                )
            assert await repository.get(denied.id) is None
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(McpServerModel)) == 1
                assert await session.scalar(select(func.count()).select_from(McpToolModel)) == 1
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
