"""Lifecycle-managed LangGraph PostgreSQL checkpointer."""

from __future__ import annotations

import sys
from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


class PostgresCheckpointer:
    """Own one AsyncPostgresSaver shared by Root and Worker graphs."""

    def __init__(self, conn_string: str) -> None:
        if not conn_string:
            raise ValueError("conn_string is required")
        self.conn_string = conn_string
        self._context: AbstractAsyncContextManager[AsyncPostgresSaver] | None = None
        self._saver: AsyncPostgresSaver | None = None

    @property
    def saver(self) -> AsyncPostgresSaver:
        if self._saver is None:
            raise RuntimeError("PostgresCheckpointer has not been started")
        return self._saver

    def get_saver(self) -> AsyncPostgresSaver:
        return self.saver

    async def start(self) -> None:
        if self._saver is not None:
            return
        context = AsyncPostgresSaver.from_conn_string(self.conn_string)
        saver = await context.__aenter__()
        try:
            await saver.setup()
        except BaseException:
            await context.__aexit__(*sys.exc_info())
            raise
        self._context = context
        self._saver = saver

    async def stop(self) -> None:
        context, self._context = self._context, None
        self._saver = None
        if context is not None:
            await context.__aexit__(None, None, None)


__all__ = ["PostgresCheckpointer"]
