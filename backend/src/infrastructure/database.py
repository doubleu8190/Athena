"""异步数据库连接与事务会话工厂。"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_POOL_SIZE = 5
DEFAULT_DATABASE_MAX_OVERFLOW = 10
# PostgreSQL 瞬态连接中断或过载类 SQLSTATE：
# 08* 连接异常、57P01/02/03 服务端关闭或重启、53300 连接数超限。
_RECOVERABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def is_recoverable_database_error(error: Exception) -> bool:
    """判定是否为可恢复的瞬态数据库/网络连接错误，避免单次闪断打崩整个消费循环。"""
    if isinstance(error, (OperationalError, InterfaceError, SqlAlchemyTimeoutError, OSError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in _RECOVERABLE_SQLSTATES
    )


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
    "is_recoverable_database_error",
]
