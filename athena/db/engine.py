"""SQLAlchemy 异步引擎与会话工厂.

管理 AsyncEngine 生命周期和 AsyncSession 创建。
使用连接池复用数据库连接，避免频繁创建/销毁。
"""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from athena.db.models import Base
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 全局引擎和会话工厂
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(db_path: str) -> None:
    """初始化数据库引擎并创建表结构.

    Args:
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
        connect_args={"check_same_thread": False},
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # 创建所有表（替代 schema.py 的 executescript）
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database_engine_initialized", db_path=db_path)


def get_session() -> AsyncSession:
    """获取 AsyncSession 实例.

    注意：AsyncSession 不等于数据库连接！
    - SQLAlchemy 内部维护连接池（pool_size=5, max_overflow=10）
    - AsyncSession 是轻量级的工作单元（Unit of Work），从连接池借出连接
    - session.close() 后连接归还池，而非关闭
    - 因此"每次操作新建 session"不会创建新连接，仅复用池中连接

    Returns:
        AsyncSession 实例，使用后需调用 session.close()

    Raises:
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
