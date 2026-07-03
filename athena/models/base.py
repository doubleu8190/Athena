"""SQLAlchemy base and engine factory.

Uses SQLite with WAL mode and StaticPool — a single connection per process
avoids "database is locked" errors while WAL enables concurrent reads.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all Athena models."""
    pass


_engines: dict[str, object] = {}
_session_makers: dict[str, object] = {}


def get_engine(db_path: str) -> AsyncEngine:
    """Get or create an async SQLAlchemy engine for the given database path.

    Uses StaticPool — one connection per process — which is correct for
    SQLite in WAL mode. WAL allows concurrent reads from different processes
    while the single writer per process avoids lock contention.
    """
    import sqlalchemy as sa

    if db_path not in _engines:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
            poolclass=sa.pool.StaticPool,
            connect_args={"check_same_thread": False},
        )

        # Enable WAL mode and foreign keys on every connection
        @event.listens_for(engine.sync_engine, "connect")
        def set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        _engines[db_path] = engine

    return _engines[db_path]


def get_session_maker(db_path: str) -> async_sessionmaker[AsyncSession]:
    """Get or create an async session maker for the given database path."""
    if db_path not in _session_makers:
        engine = get_engine(db_path)
        _session_makers[db_path] = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_makers[db_path]


async def get_session(db_path: str) -> AsyncSession:
    """Create a new async session (caller must close/rollback)."""
    maker = get_session_maker(db_path)
    return maker()
