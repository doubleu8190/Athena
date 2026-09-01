"""SQLite SQLAlchemy 异步引擎和会话工厂。

管理 AsyncEngine 生命周期和 AsyncSession 创建。
使用连接池复用数据库连接，避免频繁创建/销毁。
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from athena.infrastructure.sqlite.models import FILE_CHUNK_FTS_DDL, MEMORY_FTS_DDL, Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 普通 SQLite 读写使用短暂等待；创建 Run 时会临时切换为非阻塞锁竞争。
SQLITE_BUSY_TIMEOUT_MS = 5000

# 全局引擎和会话工厂
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(db_path: str) -> None:
    """初始化数据库引擎并创建表结构.

    参数：
        db_path: SQLite 数据库文件路径
    """
    global _engine, _session_factory

    # 确保目录存在
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(
        url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        # SQLite 特有配置
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        },
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        # 创建所有表（CREATE TABLE IF NOT EXISTS，不会修改已有表）
        # 统一 Graph 已取代 continuation 和独立文件任务；旧表不再使用。
        await conn.execute(text("DROP TABLE IF EXISTS agent_continuations"))
        await conn.execute(text("DROP TABLE IF EXISTS processing_tasks"))
        await conn.execute(text("DROP TABLE IF EXISTS file_processing_tasks"))
        await conn.execute(text("DROP INDEX IF EXISTS uq_agent_runs_active_session"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("UPDATE agent_runs SET status = 'failed', error = '旧文件任务已移除' WHERE status = 'waiting_files'")
        )
        await conn.execute(
            text("UPDATE attachments SET status = 'uploaded' WHERE status = 'queued'")
        )
        # agent_events 曾使用 event_id 作为游标。逐列迁移可以让已有数据库
        # 在不丢失事件的前提下切换到会话级 session_seq。
        columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(agent_events)"))).all()
        }
        migrations = (
            ("session_seq", "INTEGER"),
            ("stream_type", "VARCHAR"),
            ("chunk_id", "INTEGER"),
            ("is_complete", "INTEGER NOT NULL DEFAULT 0"),
            ("parent_run_id", "VARCHAR"),
            ("message_id", "VARCHAR"),
            ("attachment_id", "VARCHAR"),
        )
        for name, definition in migrations:
            if name not in columns:
                await conn.execute(
                    text(f"ALTER TABLE agent_events ADD COLUMN {name} {definition}")
                )
        await conn.execute(
            text("UPDATE agent_events SET session_seq = event_id WHERE session_seq IS NULL")
        )
        snapshot_columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(stream_snapshots)"))).all()
        }
        if "stream_type" not in snapshot_columns:
            await conn.execute(
                text("ALTER TABLE stream_snapshots ADD COLUMN stream_type VARCHAR NOT NULL DEFAULT 'answer'")
            )
        if "last_chunk_id" not in snapshot_columns:
            await conn.execute(
                text("ALTER TABLE stream_snapshots ADD COLUMN last_chunk_id INTEGER NOT NULL DEFAULT 0")
            )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_stream_chunk
                ON agent_events(session_id, stream_id, chunk_id)
                WHERE stream_id IS NOT NULL AND chunk_id IS NOT NULL
                """
            )
        )
        # 兼容旧数据库：活跃 Run 唯一索引不再把 paused 状态视为占用执行槽。
        await conn.execute(text("DROP INDEX IF EXISTS uq_agent_runs_active_session"))
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_active_session
                ON agent_runs(session_id)
                WHERE status IN (
                    'queued', 'running', 'cancel_requested',
                    'waiting_approval'
                )
                """
            )
        )
        # 创建 FTS5 虚拟表（ORM 不支持 FTS5，需原生 DDL）
        await conn.execute(text(MEMORY_FTS_DDL))
        await conn.execute(text(FILE_CHUNK_FTS_DDL))

    logger.info("database_engine_initialized", db_path=db_path)


def get_session() -> AsyncSession:
    """获取 AsyncSession 实例.

    注意：AsyncSession 不等于数据库连接！
    - SQLAlchemy 内部维护连接池（pool_size=5, max_overflow=10）
    - AsyncSession 是轻量级的工作单元（Unit of Work），从连接池借出连接
    - session.close() 后连接归还池，而非关闭
    - 因此"每次操作新建 session"不会创建新连接，仅复用池中连接

    返回值：
        AsyncSession 实例，使用后需调用 session.close()

    异常：
        RuntimeError: 如果引擎未初始化
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _session_factory()


async def close_engine() -> None:
    """关闭数据库引擎，释放连接池中所有连接."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_engine_closed")
