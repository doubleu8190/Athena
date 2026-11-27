"""MCP 目录迁移、约束和原子写入 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from uuid import uuid4

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

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
from infrastructure.security.secrets import SecretCipher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
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
        async with isolated_postgres_schema(database_url, prefix="athena_v2_mcp_catalog") as db:
            test_engine = db.engine
            expected_head = db.head_revision()
            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_head

            session_factory = db.session_factory
            now = datetime.now(UTC)
            user = User(
                id=f"usr_mcp_{suffix}",
                email=f"{suffix}@example.com",
                password_hash="unused",
                display_name=None,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                role=UserRole.SUPER_ADMIN,
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
                SecretCipher(bytes(range(32)), 2),
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

            # M1.6 软删除会标记 deleted_at、清空密文凭据三元组并硬删名下工具，
            # 读接口与工具管理列表均不再可见原记录。
            await repository.delete(actor, initial.id)
            assert await repository.get(initial.id) is None
            assert (await repository.list_admin(offset=0, limit=10)).total == 0

            # 软删除保留原主键行并释放活跃名称唯一约束（uq_mcp_servers_name_active），
            # 因此需使用新 server ID 重建，并复用被软删前已生效的 replacement.name，
            # 以真实命中部分唯一索引验证 M1.6 软删后重名复用能力。
            recreated = McpServerWrite(
                id=f"mcp_recreated_{suffix}",
                name=replacement.name,
                config=initial.config,
                secrets=initial.secrets,
            )
            recreated_server = await repository.create_with_tools(
                actor,
                recreated,
                (_tool(recreated.id, "existing", f"tool_{suffix}_existing"),),
            )
            assert recreated_server.id == recreated.id
            assert recreated_server.name == replacement.name

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
                # 活跃 MCP Server 仅保留 recreated 1 条；全表共 2 条（含 1 条软删记录 initial）。
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(McpServerModel)
                        .where(McpServerModel.deleted_at.is_(None))
                    )
                    == 1
                )
                assert await session.scalar(select(func.count()).select_from(McpServerModel)) == 2
                deleted_row = await session.get(McpServerModel, initial.id)
                assert deleted_row is not None
                assert deleted_row.deleted_at is not None
                assert deleted_row.enabled is False
                assert deleted_row.secrets_ciphertext is None
                assert deleted_row.secrets_nonce is None
                assert deleted_row.key_version is None
                # 仅 recreated 的 1 条工具存在，initial 软删与 denied 回滚均未遗留工具记录。
                assert await session.scalar(select(func.count()).select_from(McpToolModel)) == 1
                assert await session.scalar(select(McpToolModel.server_id)) == recreated.id

    asyncio.run(scenario())
