"""PostgreSQL session locks that serialize LangGraph calls per thread."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection


class PostgresGraphThreadLock():
    """Hold a cross-process advisory lock for one complete graph invocation."""

    def __init__(self, connection_string: str, *, poll_interval: float = 0.1) -> None:
        if not connection_string.strip() or poll_interval <= 0:
            raise ValueError("connection string and positive poll interval are required")
        self._connection_string = connection_string
        self._poll_interval = poll_interval

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncIterator[None]:
        """Wait until this process owns the thread lock, then release it on exit."""
        if not thread_id.strip():
            raise ValueError("thread_id is required")

        connection = await AsyncConnection.connect(self._connection_string)
        acquired = False
        try:
            while not acquired:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                        (thread_id,),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
                acquired = bool(row and row[0])
                if not acquired:
                    await asyncio.sleep(self._poll_interval)
            yield
        finally:
            try:
                if acquired:
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                            (thread_id,),
                        )
                        await cursor.fetchone()
                    await connection.commit()
            finally:
                await connection.close()


__all__ = ["PostgresGraphThreadLock"]
