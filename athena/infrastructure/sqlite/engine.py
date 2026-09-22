"""PostgreSQL 异步引擎和统一数据库结构初始化。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from athena.infrastructure.sqlite.models import Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def initialize_postgres_engine(
    database_url: str, *, pool_size: int = 10, max_overflow: int = 10
) -> None:
    """连接 PostgreSQL 并创建完整的单库 schema。"""
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                source_memory_id VARCHAR NOT NULL, target_memory_id VARCHAR NOT NULL,
                relation_type VARCHAR NOT NULL, created_at VARCHAR NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (source_memory_id, target_memory_id, relation_type)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_processing_jobs (
                job_id VARCHAR PRIMARY KEY, turn_id VARCHAR NOT NULL UNIQUE,
                session_id VARCHAR NOT NULL, status VARCHAR NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0, available_at VARCHAR NOT NULL,
                payload_json TEXT NOT NULL, result_json TEXT, error_json TEXT,
                created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_memory_jobs_queue
            ON memory_processing_jobs(status, available_at)
        """))
        # Portable keyword indexes; PostgreSQL FTS is intentionally kept in the
        # repository query layer so the schema remains easy to rebuild.
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_fts (
                memory_id VARCHAR PRIMARY KEY, content TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS file_chunk_fts (
                chunk_id VARCHAR PRIMARY KEY, attachment_id VARCHAR NOT NULL,
                content TEXT NOT NULL
            )
        """))
    logger.info(
        "postgres_database_initialized", database_url=database_url.split("@")[-1]
    )


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError(
            "Database engine not initialized. Call Database.connect() first."
        )
    return _session_factory()


async def close_postgres_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    logger.info("postgres_database_closed")
