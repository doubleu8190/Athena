from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from athena.config.settings import Settings
from athena.core.memory.contracts import CompletedTurn
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.infrastructure.postgre.engine import (
    close_postgres_engine,
    get_session,
    initialize_postgres_engine,
)
from athena.infrastructure.postgre.repositories.memory_job_repository import MemoryJobRepository
from athena.infrastructure.postgre.repositories.memory_repository import PostgresMemoryRepository


class _VectorStore:
    async def initialize(self) -> None:
        pass

    async def add(self, memory_id: str, content: str, metadata: dict) -> None:
        pass


@pytest.fixture
async def jobs(tmp_path):
    await initialize_postgres_engine(str(tmp_path / "jobs.db"))
    repository = MemoryJobRepository()
    yield repository
    await close_postgres_engine()


def payload(turn_id: str = "turn-1") -> dict:
    return CompletedTurn(
        turn_id=turn_id, session_id="session-1", user_text="记住使用 PostgreSQL"
    ).model_dump(mode="json")


async def status(turn_id: str) -> tuple[str, int]:
    async with get_session() as session:
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
    async with get_session() as session:
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
