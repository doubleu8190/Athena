from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from athena.config.settings import Settings
from athena.core.memory.contracts import CompletedTurn
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.infrastructure.sqlite.engine import (
    close_sqlite_engines,
    get_memory_database_session,
    get_core_session,
    initialize_sqlite_engines,
)
from athena.infrastructure.sqlite.repositories.memory_job_repository import MemoryJobRepository
from athena.infrastructure.sqlite.repositories.memory_repository import SQLiteMemoryRepository


class _VectorStore:
    async def initialize(self) -> None:
        pass

    async def add(self, memory_id: str, content: str, metadata: dict) -> None:
        pass


@pytest.fixture
async def jobs(tmp_path):
    await initialize_sqlite_engines(str(tmp_path / "jobs.db"))
    repository = MemoryJobRepository()
    yield repository
    await close_sqlite_engines()


def payload(turn_id: str = "turn-1") -> dict:
    return CompletedTurn(
        turn_id=turn_id, session_id="session-1", user_text="记住使用 SQLite"
    ).model_dump(mode="json")


async def status(turn_id: str) -> tuple[str, int]:
    async with get_core_session() as session:
        row = (
            await session.execute(
                text("SELECT status, attempt FROM memory_processing_jobs WHERE turn_id=:id"),
                {"id": turn_id},
            )
        ).one()
    return row.status, row.attempt


@pytest.mark.asyncio
async def test_duplicate_enqueue_is_idempotent(jobs):
    assert await jobs.enqueue_job(payload()) is True
    assert await jobs.enqueue_job(payload()) is False
    assert await status("turn-1") == ("queued", 0)


@pytest.mark.asyncio
async def test_failed_job_can_be_retried(jobs):
    await jobs.enqueue_job(payload())
    claimed = await jobs.claim_next_job()
    assert claimed and claimed["attempt"] == 1
    await jobs.mark_job_failed("turn-1", "temporary", retry=True)
    async with get_core_session() as session:
        await session.execute(
            text("UPDATE memory_processing_jobs SET available_at='0001-01-01' WHERE turn_id='turn-1'")
        )
        await session.commit()
    retried = await jobs.claim_next_job()
    assert retried and retried["attempt"] == 2


@pytest.mark.asyncio
async def test_running_job_is_recovered_after_crash(jobs):
    await jobs.enqueue_job(payload())
    assert await jobs.claim_next_job()
    assert (await status("turn-1"))[0] == "running"
    assert await jobs.recover_interrupted_jobs() == 1
    assert (await status("turn-1"))[0] == "retry"
    assert await jobs.claim_next_job()


@pytest.mark.asyncio
async def test_job_cannot_be_claimed_twice(jobs):
    await jobs.enqueue_job(payload())
    first, second = await asyncio.gather(
        jobs.claim_next_job(), jobs.claim_next_job()
    )
    assert sum(item is not None for item in (first, second)) == 1


@pytest.mark.asyncio
async def test_jobs_use_separate_database_when_configured(tmp_path):
    await initialize_sqlite_engines(str(tmp_path / "core.db"), str(tmp_path / "memory.db"))
    try:
        assert await MemoryJobRepository().enqueue_job(payload("split-turn")) is True
        async with get_core_session() as session:
            core_memory_tables = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name IN ('memories', 'memory_relations', 'memory_processing_jobs')"
                    )
                )
            ).scalars().all()
        async with get_memory_database_session() as session:
            memory_count = (
                await session.execute(
                    text("SELECT count(*) FROM memory_processing_jobs")
                )
            ).scalar_one()
        assert core_memory_tables == []
        assert memory_count == 1
    finally:
        await close_sqlite_engines()


@pytest.mark.asyncio
async def test_memory_manager_writes_to_configured_memory_database(tmp_path):
    await initialize_sqlite_engines(str(tmp_path / "core.db"), str(tmp_path / "memory.db"))
    try:
        manager = LongTermMemoryService(
            settings=Settings(),
            repository=SQLiteMemoryRepository(),
            vector_store=_VectorStore(),
        )
        await manager.add_memory("remember this", metadata={"session_id": "s1"})

        async with get_memory_database_session() as session:
            assert (
                await session.execute(text("SELECT count(*) FROM memories"))
            ).scalar_one() == 1
        async with get_core_session() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM sqlite_master WHERE type='table' "
                        "AND name='memories'"
                    )
                )
            ).scalar_one() == 0
    finally:
        await close_sqlite_engines()


@pytest.mark.asyncio
async def test_recover_retries_jobs_failed_by_unconfigured_worker(jobs):
    await jobs.enqueue_job(payload())
    await jobs.claim_next_job()
    await jobs.mark_job_failed(
        "turn-1", "memory job worker workflow is not configured", retry=False
    )

    assert await jobs.recover_interrupted_jobs() == 1
    recovered = await jobs.claim_next_job()

    assert recovered is not None
    assert recovered["attempt"] == 1


@pytest.mark.asyncio
async def test_split_database_creates_only_final_memory_schema(tmp_path):
    core_path = str(tmp_path / "core.db")
    memory_path = str(tmp_path / "memory.db")
    await initialize_sqlite_engines(core_path, memory_path)
    try:
        async with get_memory_database_session() as session:
            memory_tables = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name IN ('memories', 'memory_relations', 'memory_processing_jobs', 'memory_fts') "
                        "ORDER BY name"
                    )
                )
            ).scalars().all()
        assert memory_tables == [
            "memories",
            "memory_fts",
            "memory_processing_jobs",
            "memory_relations",
        ]
        async with get_core_session() as session:
            core_memory_tables = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name IN ('memories', 'memory_relations', 'memory_processing_jobs', 'memory_fts')"
                    )
                )
            ).scalars().all()
        assert core_memory_tables == []
    finally:
        await close_sqlite_engines()
