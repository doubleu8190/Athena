"""可配置 PostgreSQL 集成测试入口。"""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.src.domain.sessions import Session, SessionStatus
from backend.src.domain.tools import (
    JsonSchema,
    MCPServer,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionMode,
)
from backend.src.infrastructure.persistence.postgres.models import (
    Base,
    MessageModel,
    SessionModel,
    MCPServerModel,
    ToolModel,
)
from backend.src.infrastructure.persistence.postgres.repositories import (
    PostgresSessionRepository,
    PostgresMCPServerRepository,
    PostgresToolConfigRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_repository_against_postgresql() -> None:
    """在隔离测试数据库中验证 Session CRUD 和软删除。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    engine = create_async_engine(DATABASE_URL)
    session_id = f"integration-session-{uuid4().hex}"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[SessionModel.__table__, MessageModel.__table__],
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = PostgresSessionRepository(factory)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        session = Session(
            id=session_id,
            title="Integration",
            status=SessionStatus.IDLE,
            run_id=None,
            created_at=now,
            updated_at=now,
        )
        await repository.create(session)
        persisted = await repository.get(session.id)
        assert persisted is not None
        assert persisted.title == "Integration"
        assert await repository.delete(session.id)
        assert await repository.get(session.id) is None
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_and_mcp_repositories_against_postgresql() -> None:
    """在 PostgreSQL 中验证工具治理和 MCP 配置的目标 repository。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    engine = create_async_engine(DATABASE_URL)
    tool_name = f"integration-tool-{uuid4().hex}"
    server_name = f"integration-server-{uuid4().hex}"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[ToolModel.__table__, MCPServerModel.__table__],
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tools = PostgresToolConfigRepository(factory)
        servers = PostgresMCPServerRepository(factory)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        config = ToolConfig(
            tool_name=tool_name,
            execution_mode=ToolExecutionMode.NATIVE,
            server_name=None,
            remote_name=None,
            description="Integration",
            parameters=JsonSchema({"type": "object"}),
            risk_level=RiskLevel.LOW,
            require_approval=False,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        await tools.upsert(config)
        persisted = await tools.get(tool_name)
        assert persisted is not None
        assert persisted.description == "Integration"
        updated = replace(config, enabled=False)
        await tools.update(updated)
        assert (await tools.get(tool_name)).enabled is False

        server = MCPServer(
            name=server_name,
            config=MCPServerConfig("python", (), {}, None, "none", True),
            created_at=now,
        )
        await servers.upsert(server)
        assert await servers.get(server_name) is not None
        assert await servers.delete(server_name)
        assert await servers.get(server_name) is None
    finally:
        await engine.dispose()
