"""SQLite 异步引擎和最终数据库结构初始化。"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from athena.infrastructure.sqlite.models import FILE_CHUNK_FTS_DDL, MEMORY_FTS_DDL, Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)

SQLITE_BUSY_TIMEOUT_MS = 5000

MEMORY_RELATIONS_DDL = """
CREATE TABLE IF NOT EXISTS memory_relations (
    source_memory_id VARCHAR NOT NULL,
    target_memory_id VARCHAR NOT NULL,
    relation_type VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_memory_id, target_memory_id, relation_type)
)
"""

MEMORY_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS memory_processing_jobs (
    job_id VARCHAR PRIMARY KEY,
    turn_id VARCHAR NOT NULL UNIQUE,
    session_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at VARCHAR NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
)
"""

MEMORY_JOBS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_memory_jobs_queue
ON memory_processing_jobs(status, available_at)
"""

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_memory_engine = None
_memory_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _configure_sqlite_connection(conn: AsyncConnection) -> None:
    """配置 SQLite 连接的并发与日志模式。

    参数：
        conn: 已开启事务的异步连接。

    返回：
        None。

    异常：
        SQLite 无法设置 PRAGMA 时向上抛出异常。
    """
    await conn.execute(text("PRAGMA journal_mode=WAL"))
    await conn.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))


async def _create_memory_schema(conn: AsyncConnection) -> None:
    """创建记忆数据库的最终表结构。

    参数：
        conn: 已开启事务的记忆数据库连接。

    返回：
        None。

    异常：
        当前表结构无法创建时向上抛出异常。
    """
    await conn.run_sync(
        lambda sync_conn: Base.metadata.tables["memories"].create(
            sync_conn, checkfirst=True
        )
    )
    await conn.execute(text(MEMORY_RELATIONS_DDL))
    await conn.execute(text(MEMORY_JOBS_DDL))
    await conn.execute(text(MEMORY_JOBS_INDEX_DDL))
    await conn.execute(text(MEMORY_FTS_DDL))
    # create_all 不会向已存在的 SQLite 表补列。这里仅做可前向兼容的增列，
    # 让已安装实例也获得独立的事实有效性和修订字段。
    columns = {
        row[1]
        for row in (await conn.execute(text("PRAGMA table_info(memories)"))).fetchall()
    }
    migrations = {
        "validity_status": "ALTER TABLE memories ADD COLUMN validity_status VARCHAR NOT NULL DEFAULT 'valid'",
        "valid_until": "ALTER TABLE memories ADD COLUMN valid_until VARCHAR",
        "revision_of": "ALTER TABLE memories ADD COLUMN revision_of VARCHAR",
        "revision": "ALTER TABLE memories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    }
    for name, statement in migrations.items():
        if name not in columns:
            await conn.execute(text(statement))


async def initialize_sqlite_engines(
    db_path: str, memory_db_path: str | None = None
) -> None:
    """初始化核心库和可选的独立记忆库。

    参数：
        db_path: 核心 SQLite 文件路径。
        memory_db_path: 记忆 SQLite 文件路径；为空时与核心库共用同一文件。

    返回：
        None。

    异常：
        数据库文件无法创建或最终结构无法初始化时向上抛出异常。
    """
    global _engine, _session_factory, _memory_engine, _memory_session_factory

    split_memory = bool(
        memory_db_path
        and os.path.abspath(memory_db_path) != os.path.abspath(db_path)
    )
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    _engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        pool_size=5,
        max_overflow=10,
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        },
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await _configure_sqlite_connection(conn)
        core_tables = [
            table
            for table in Base.metadata.sorted_tables
            if not (split_memory and table.name == "memories")
        ]
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=core_tables)
        )
        await conn.execute(text(FILE_CHUNK_FTS_DDL))
        if not split_memory:
            await _create_memory_schema(conn)

    target_memory_path = memory_db_path or db_path
    if split_memory:
        os.makedirs(os.path.dirname(target_memory_path) or ".", exist_ok=True)
        _memory_engine = create_async_engine(
            f"sqlite+aiosqlite:///{target_memory_path}",
            echo=False,
            pool_size=1,
            max_overflow=0,
            connect_args={
                "check_same_thread": False,
                "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
            },
        )
        _memory_session_factory = async_sessionmaker(
            _memory_engine, expire_on_commit=False
        )
        async with _memory_engine.begin() as conn:
            await _configure_sqlite_connection(conn)
            await _create_memory_schema(conn)

    logger.info(
        "database_engine_initialized", db_path=db_path, memory_db_path=target_memory_path
    )


def get_core_session() -> AsyncSession:
    """获取核心数据库会话。

    参数：
        无。

    返回：
        可用于核心数据库读写的异步会话。

    异常：
        RuntimeError: 引擎尚未初始化。
    """
    if _session_factory is None:
        raise RuntimeError(
            "Database engine not initialized. Call initialize_sqlite_engines() first."
        )
    return _session_factory()


def get_memory_database_session() -> AsyncSession:
    """获取记忆数据库会话。

    参数：
        无。

    返回：
        独立记忆库会话，未拆库时返回核心库会话。

    异常：
        RuntimeError: 引擎尚未初始化。
    """
    factory = _memory_session_factory or _session_factory
    if factory is None:
        raise RuntimeError(
            "Database engine not initialized. Call initialize_sqlite_engines() first."
        )
    return factory()


async def close_sqlite_engines() -> None:
    """关闭核心和记忆数据库引擎。

    参数：
        无。

    返回：
        None。

    异常：
        引擎释放失败时向上抛出异常。
    """
    global _engine, _session_factory, _memory_engine, _memory_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    if _memory_engine is not None:
        await _memory_engine.dispose()
        _memory_engine = None
        _memory_session_factory = None
    logger.info("database_engine_closed")
