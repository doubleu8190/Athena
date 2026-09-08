from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from athena.core.memory.contracts import CompletedTurn
from athena.infrastructure.sqlite.engine import close_engine, get_session, init_engine
from athena.infrastructure.sqlite.memory_job_repository import MemoryJobRepository


@pytest.fixture
async def jobs(tmp_path):
    await init_engine(str(tmp_path / "jobs.db"))
    repository = MemoryJobRepository()
    yield repository
    await close_engine()


def payload(turn_id: str = "turn-1") -> dict:
    return CompletedTurn(
        turn_id=turn_id, session_id="session-1", user_text="记住使用 SQLite"
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
    assert await jobs.enqueue(payload()) is True
    assert await jobs.enqueue(payload()) is False
    assert await status("turn-1") == ("queued", 0)


@pytest.mark.asyncio
async def test_failed_job_can_be_retried(jobs):
    await jobs.enqueue(payload())
    claimed = await jobs.claim_next()
    assert claimed and claimed["attempt"] == 1
    await jobs.mark_failed("turn-1", "temporary", retry=True)
    async with get_session() as session:
        await session.execute(
            text("UPDATE memory_processing_jobs SET available_at='0001-01-01' WHERE turn_id='turn-1'")
        )
        await session.commit()
    retried = await jobs.claim_next()
    assert retried and retried["attempt"] == 2


@pytest.mark.asyncio
async def test_running_job_is_recovered_after_crash(jobs):
    await jobs.enqueue(payload())
    assert await jobs.claim_next()
    assert (await status("turn-1"))[0] == "running"
    assert await jobs.recover_interrupted() == 1
    assert (await status("turn-1"))[0] == "retry"
    assert await jobs.claim_next()


@pytest.mark.asyncio
async def test_job_cannot_be_claimed_twice(jobs):
    await jobs.enqueue(payload())
    first, second = await asyncio.gather(jobs.claim_next(), jobs.claim_next())
    assert sum(item is not None for item in (first, second)) == 1
