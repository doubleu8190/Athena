"""可配置 PostgreSQL 集成测试入口。"""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.sessions import Session, SessionStatus
from domain.tools import (
    JsonSchema,
    MCPServer,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionMode,
)
from infrastructure.persistence.postgres.models import (
    Base,
    MessageModel,
    ProjectModel,
    SessionModel,
    MCPServerModel,
    ToolModel,
)
from infrastructure.persistence.postgres.repositories import (
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
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        MessageModel.__table__,
                    ],
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = PostgresSessionRepository(factory)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        project_id = f"integration-project-{uuid4().hex}"
        async with factory() as db, db.begin():
            db.add(
                ProjectModel(
                    id=project_id,
                    name="Integration project",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
        session = Session(
            id=session_id,
            project_id=project_id,
            title="Integration",
            status=SessionStatus.IDLE,
            run_id=None,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
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

@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_access_and_expiration_against_postgresql() -> None:
    """验证真实 PostgreSQL 并发访问累加及两种期限过滤；未配置数据库时跳过。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    import asyncio
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete, update
    from domain.memory import MemoryListRequest, MemorySearchRequest, MemoryWriteCommand
    from infrastructure.persistence.postgres.models import MemoryModel
    from infrastructure.persistence.postgres.repositories import PostgresMemoryRepository

    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresMemoryRepository(factory)
    prefix = f"memory-test-{uuid4().hex}"
    ids = [f"{prefix}-{suffix}" for suffix in ("active", "ttl", "validity")]
    try:
        for memory_id in ids:
            await repository.create_memory(MemoryWriteCommand(prefix, operation_key=memory_id))
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        async with factory() as session, session.begin():
            await session.execute(update(MemoryModel).where(MemoryModel.id == ids[1]).values(expires_at=past))
            await session.execute(update(MemoryModel).where(MemoryModel.id == ids[2]).values(valid_until=past))
        assert [item.id for item in await repository.search(MemorySearchRequest(prefix))] == ids[:1]
        before = await repository.get(ids[0])
        await asyncio.gather(*(repository.record_access([ids[0], ids[0]]) for _ in range(5)))
        after = await repository.get(ids[0])
        assert before.access_count == 0 and after.access_count == 5
        assert after.last_accessed_at is not None
        assert after.last_observed_at == before.last_observed_at
        page = await repository.list_memories(MemoryListRequest(expired_only=True, limit=10000))
        assert set(ids[1:]) <= {item.id for item in page.items}
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(MemoryModel).where(MemoryModel.id.in_(ids)))
        await engine.dispose()
