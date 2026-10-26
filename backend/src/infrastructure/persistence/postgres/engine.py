"""Target-owned PostgreSQL engine and lifecycle resource."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base
from .options import PostgresOptions


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
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                # Fresh databases need the same zhparser configuration used by
                # the generated Chinese full-text columns below.
                await connection.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": "athena:chinese-text-search-configuration"},
                )
                await connection.exec_driver_sql(
                    "CREATE EXTENSION IF NOT EXISTS zhparser"
                )
                has_chinese_config = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_ts_config "
                        "WHERE cfgname = 'chinese')"
                    )
                )
                if not has_chinese_config:
                    await connection.exec_driver_sql(
                        "CREATE TEXT SEARCH CONFIGURATION chinese "
                        "(PARSER = zhparser)"
                    )
                    aliases = (
                        await connection.execute(
                            text(
                                "SELECT alias FROM ts_token_type('zhparser') "
                                "ORDER BY tokid"
                            )
                        )
                    ).scalars().all()
                    if not aliases:
                        raise RuntimeError(
                            "zhparser returned no token types for chinese configuration"
                        )
                    quote = connection.dialect.identifier_preparer.quote
                    token_aliases = ", ".join(quote(alias) for alias in aliases)
                    await connection.exec_driver_sql(
                        "ALTER TEXT SEARCH CONFIGURATION chinese "
                        f"ADD MAPPING FOR {token_aliases} WITH simple"
                    )
                await connection.run_sync(Base.metadata.create_all)

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
