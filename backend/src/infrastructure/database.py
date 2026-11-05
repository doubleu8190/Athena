"""异步数据库连接与事务会话工厂。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_POOL_SIZE = 5
DEFAULT_DATABASE_MAX_OVERFLOW = 10


def _validate_pool_configuration(
    *,
    pool_size: int,
    max_overflow: int,
) -> None:
    """校验业务连接池容量，避免无界或无效配置。"""
    values = (
        ("pool_size", pool_size, 1),
        ("max_overflow", max_overflow, 0),
    )
    for name, value, minimum in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name}_invalid")


class Database:
    """持有一个应用进程的业务数据库连接资源。"""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = DEFAULT_DATABASE_POOL_SIZE,
        max_overflow: int = DEFAULT_DATABASE_MAX_OVERFLOW,
    ) -> None:
        """根据数据库 URL 创建有界业务连接池。"""
        _validate_pool_configuration(
            pool_size=pool_size,
            max_overflow=max_overflow,
        )
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

    async def dispose(self) -> None:
        """释放业务连接池。"""
        await self.engine.dispose()


__all__ = [
    "DEFAULT_DATABASE_MAX_OVERFLOW",
    "DEFAULT_DATABASE_POOL_SIZE",
    "Database",
]
