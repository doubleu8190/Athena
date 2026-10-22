"""Target-owned PostgreSQL engine and lifecycle resource."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base
from .options import PostgresOptions
from .schema_migrations import apply_schema_migrations


class PostgresEngine:
    """Owns the target SQLAlchemy engine and schema lifecycle."""

    def __init__(self, options: PostgresOptions, *, create_schema: bool | None = None) -> None:
        self.options = options
        self.create_schema = self.options.create_schema if create_schema is None else create_schema
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def start(self) -> None:
        if self.engine is not None:
            return
        self.engine = create_async_engine(
            self.options.url,
            pool_pre_ping=True,
            pool_size=self.options.pool_size,
            max_overflow=self.options.max_overflow,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        if self.create_schema:
            async with self.engine.begin() as connection:
                # The extension is optional for local schema smoke tests. The
                # vector columns still require it in a production PostgreSQL.
                try:
                    await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                except Exception:
                    await connection.rollback()
                await connection.run_sync(Base.metadata.create_all)
                await connection.run_sync(apply_schema_migrations)

    async def stop(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
        self.engine = None
        self.session_factory = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self.session_factory is None:
            raise RuntimeError("PostgresEngine has not been started")
        async with self.session_factory() as session:
            yield session


__all__ = ["PostgresEngine"]
