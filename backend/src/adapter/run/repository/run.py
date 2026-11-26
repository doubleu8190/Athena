"""Run 原子命令的 PostgreSQL 门面仓储。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.repository.context_events import RunContextEventsMixin
from adapter.run.repository.lifecycle import RunLifecycleMixin
from adapter.run.repository.tool_calls import RunToolCallsMixin
from domain.run.ports import RunRepository


class SqlAlchemyRunRepository(
    RunLifecycleMixin,
    RunToolCallsMixin,
    RunContextEventsMixin,
    RunRepository,
):
    """按职责组合生命周期、工具批处理与上下文事件子仓储的统一门面。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        message_repository: SqlAlchemyMessageRepository,
    ) -> None:
        """保存共享数据库工厂和消息事务内部写入端口。"""
        self._session_factory = session_factory
        # 统一使用 _message_repo 命名持有时效消息子仓储
        self._message_repo = message_repository


__all__ = ["SqlAlchemyRunRepository"]
