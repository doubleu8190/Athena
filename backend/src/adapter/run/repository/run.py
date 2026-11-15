"""Run 原子命令的 PostgreSQL 门面仓储。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.repository.context_events import RunContextEventsMixin
from adapter.run.repository.lifecycle import RunLifecycleMixin
from adapter.run.repository.tool_calls import RunToolCallsMixin
from domain.memory import MemoryRunWakeupPort
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
        *,
        memory_wakeup: MemoryRunWakeupPort | None = None,
        memory_lifecycle_enabled: bool = False,
    ) -> None:
        """保存共享数据库工厂和消息事务内部写入端口。"""
        self._session_factory = session_factory
        self._messages = message_repository
        self._memory_wakeup = memory_wakeup
        self._memory_lifecycle_enabled = memory_lifecycle_enabled


__all__ = ["SqlAlchemyRunRepository"]
