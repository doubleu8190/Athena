"""迁移期 PostgreSQL Unit of Work。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class PostgresUnitOfWork:
    """管理一个用例范围内的异步数据库会话和事务。

    Repository 通过 ``session_factory`` 注入独立会话；跨 repository 的原子写入
    使用本类提供的 ``session`` 和事务边界。它不暴露旧 ``Database`` 门面。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> "PostgresUnitOfWork":
        """打开数据库会话。"""
        self.session = self._session_factory()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """根据异常提交或回滚并释放会话。"""
        if self.session is None:
            return
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """提供一个事务范围，并在离开时提交或回滚。"""
        async with self._session_factory() as session:
            async with session.begin():
                yield session


__all__ = ["PostgresUnitOfWork"]
