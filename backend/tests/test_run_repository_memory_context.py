"""Run 固定记忆引用的原子持久化与 fencing 测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import DateTime, func, literal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import UserModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.run.model import RunModel
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from domain.memory.lifecycle import (
    RunMemoryContextReference,
    RunMemoryContextState,
)
from domain.run.model import InitializeRunMemoryContextCommand, RunStatus


def test_memory_context_snapshot_is_atomic_empty_safe_and_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次引用固定后不可覆盖；空快照仍初始化，过期租约不得写入。"""

    clock_now = datetime.now(UTC)
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(clock_now, type_=DateTime(timezone=True)),
    )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyRunRepository(
            factory,
            SqlAlchemyMessageRepository(factory),
        )
        now = clock_now
        async with factory() as session, session.begin():
            session.add(
                UserModel(
                    id="user-memory-context",
                    email="memory-context@example.com",
                    password_hash="hash",
                    display_name=None,
                    status="active",
                    created_at=now,
                    updated_at=now,
                    deleted_time=None,
                )
            )
            session.add(
                SessionModel(
                    id="session-memory-context",
                    owner_id="user-memory-context",
                    title=None,
                    project_id=None,
                    last_message_sequence=1,
                    created_at=now,
                    updated_at=now,
                    deleted_time=None,
                )
            )
            session.add(
                RunModel(
                    id="run-memory-context",
                    session_id="session-memory-context",
                    owner_id="user-memory-context",
                    status=RunStatus.RUNNING.value,
                    trigger_message_id="message-memory-context",
                    context_cutoff_sequence=1,
                    retry_of_run_id=None,
                    model_config_version_id="profile-memory-context",
                    main_prompt_version_id="prompt-main",
                    compression_prompt_version_id="prompt-compression",
                    prompt_template_version="run-v2",
                    event_sequence=0,
                    execution_epoch=2,
                    worker_id="worker-memory-context",
                    lease_expires_at=now + timedelta(minutes=2),
                    execution_started_at=now,
                    execution_deadline_at=now + timedelta(minutes=1),
                    queued_at=now,
                    first_started_at=now,
                    created_at=now,
                    updated_at=now,
                    memory_context_state={},
                    usage={},
                )
            )

        initial = RunMemoryContextState(
            initialized=True,
            references=(
                RunMemoryContextReference(
                    memory_id="memory-initial",
                    version=1,
                    scope_key="personal",
                ),
            ),
        )
        fixed = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=initial,
            )
        )
        assert fixed == initial

        competing = RunMemoryContextState(
            initialized=True,
            references=(
                RunMemoryContextReference(
                    memory_id="memory-newer",
                    version=4,
                    scope_key="personal",
                ),
            ),
        )
        repeated = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=competing,
            )
        )
        assert repeated == initial

        async with factory() as session, session.begin():
            row = await session.get(RunModel, "run-memory-context")
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=competing,
            )
        )
        assert expired is None

        async with factory() as session, session.begin():
            row = await session.get(RunModel, "run-memory-context")
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            row.memory_context_state = {}
        empty = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=RunMemoryContextState(initialized=True),
            )
        )
        assert empty == RunMemoryContextState(initialized=True)
        after_empty = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=competing,
            )
        )
        assert after_empty == empty

        async with factory() as session, session.begin():
            row = await session.get(RunModel, "run-memory-context")
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        fenced = await repository.initialize_memory_context(
            InitializeRunMemoryContextCommand(
                run_id="run-memory-context",
                worker_id="worker-memory-context",
                execution_epoch=2,
                context_state=competing,
            )
        )
        assert fenced is None
        await engine.dispose()

    asyncio.run(scenario())
