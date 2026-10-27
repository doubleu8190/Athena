"""首次消息创建会话的 PostgreSQL 事务与并发回归测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from application.common.responses import CommandEnqueueResponse
from application.runs import CommandRejectedError, RunCommandService
from domain.memory.suppression import PROJECT_TURN_SUPPRESSION_FINGERPRINT
from infrastructure.persistence.postgres.models import (
    AgentCommandModel,
    AgentRunModel,
    Base,
    MemoryJobScopeSnapshotModel,
    MemorySourceSuppressionModel,
    ProjectModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresRunRepository,
    PostgresSessionRepository,
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """在随机 schema 中建表并清理，防止回归测试修改开发库业务数据。"""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"first_message_{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(url)
    engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    tables = [
        ProjectModel.__table__,
        SessionModel.__table__,
        AgentCommandModel.__table__,
        AgentRunModel.__table__,
        MemoryJobScopeSnapshotModel.__table__,
        MemorySourceSuppressionModel.__table__,
    ]
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            # public 中已存在同名表，必须明确在隔离 schema 内重新创建。
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=tables, checkfirst=False
                )
            )
            assert await connection.scalar(text("SELECT current_schema()")) == schema
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc).isoformat()
        async with factory() as session, session.begin():
            session.add(ProjectModel(
                id="project-1", name="首次发送", created_at=now, updated_at=now
            ))
        yield factory
    finally:
        await engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_first_message_retries_reuse_one_session_and_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """相同首条命令并发重试只能提交一份会话、Run 和项目范围快照。"""
    sessions = PostgresSessionRepository(session_factory)
    runs = PostgresRunRepository(session_factory)
    service = RunCommandService(sessions, runs, runs, id_factory=lambda: uuid4().hex)

    async def submit() -> CommandEnqueueResponse:
        """让并发请求携带相同的客户端命令和消息身份。"""
        return await service.submit(
            None, project_id="project-1", command_id="first-command",
            message="请帮我整理记忆系统。", message_id="first-message",
        )

    first, retry = await asyncio.gather(submit(), submit())

    assert first.session_id == retry.session_id
    assert first.run_id == retry.run_id
    assert sorted([first.deduplicated, retry.deduplicated]) == [False, True]
    saved = await sessions.get(first.session_id)
    assert saved is not None
    assert saved.project_id == "project-1"
    assert saved.title == "整理记忆系统"
    async with session_factory() as session:
        for model in (
            SessionModel, AgentCommandModel, AgentRunModel, MemoryJobScopeSnapshotModel
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1

    # 后续命令重放只复用原结果，不回写创建时的自动标题。
    await sessions.rename(saved.rename("手动标题", now=datetime.now(timezone.utc)))
    replay = await submit()
    assert replay.session_id == first.session_id
    renamed = await sessions.get(first.session_id)
    assert renamed is not None
    assert renamed.title == "手动标题"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejected_first_run_rolls_back_the_new_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """新会话插入后若 Run 范围被拒绝，事务必须撤销该空会话。"""
    now = datetime.now(timezone.utc).isoformat()
    async with session_factory() as session, session.begin():
        session.add(MemorySourceSuppressionModel(
            source_type="project_turn", source_id="blocked-run",
            item_fingerprint=PROJECT_TURN_SUPPRESSION_FINGERPRINT,
            created_at=now,
        ))
    runs = PostgresRunRepository(session_factory)
    service = RunCommandService(
        PostgresSessionRepository(session_factory), runs, runs,
        id_factory=iter(["rejected-session", "blocked-run"]).__next__,
    )

    with pytest.raises(CommandRejectedError):
        await service.submit(
            None, project_id="project-1", command_id="rejected-command",
            message="整理记忆系统", message_id="rejected-message",
        )

    async with session_factory() as session:
        for model in (
            SessionModel, AgentCommandModel, AgentRunModel, MemoryJobScopeSnapshotModel
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
