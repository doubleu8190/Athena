"""PostgreSQL acceptance for cross-process Worker thread exclusivity."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

from infrastructure.persistence.postgres.graph_thread_lock import (
    PostgresGraphThreadLock,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_worker_thread_lock_serializes_separate_app_instances() -> None:
    """A second app cannot enter the same Worker thread until the owner exits."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg"}:
        url = url.set(drivername="postgresql")
    connection_string = make_conninfo(
        **{
            "host": url.host or "",
            "port": url.port or 5432,
            "dbname": url.database or "",
            "user": url.username or "",
            "password": url.password or "",
            **{
                name: value
                for name, value in url.query.items()
                if isinstance(value, (str, int))
            },
        }
    )
    thread_id = f"thread-lock:{uuid4().hex}"
    first_lock = PostgresGraphThreadLock(connection_string, poll_interval=0.01)
    second_lock = PostgresGraphThreadLock(connection_string, poll_interval=0.01)
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    active = 0
    maximum_active = 0

    async def use_lock(lock, entered, release) -> None:
        nonlocal active, maximum_active
        async with lock.hold(thread_id):
            active += 1
            maximum_active = max(maximum_active, active)
            entered.set()
            await release.wait()
            active -= 1

    first = asyncio.create_task(
        use_lock(first_lock, first_entered, release_first)
    )
    second = None
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=3)
        second = asyncio.create_task(
            use_lock(second_lock, second_entered, release_second)
        )
        await asyncio.sleep(0.05)
        assert not second_entered.is_set()
        assert not second.done()

        release_first.set()
        await asyncio.wait_for(second_entered.wait(), timeout=3)
        assert maximum_active == 1
        release_second.set()
        await asyncio.gather(first, second)
        assert maximum_active == 1
    finally:
        release_first.set()
        release_second.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
