"""事件发布、历史查询和实时订阅的统一端口。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .entities import ApplicationEvent


class EventPort(Protocol):
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """持久化事件并通知当前订阅者。"""
        ...

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        """只通知实时订阅者，不写入历史。"""
        ...

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None,
        limit: int = 256,
    ) -> list[ApplicationEvent]:
        """按会话序号读取历史事件。"""
        ...

    async def list_events_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> tuple[list[ApplicationEvent], bool]:
        """读取指定运行的事件及是否还有后续记录。"""
        ...

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """订阅会话事件，并返回订阅队列和历史水位。"""
        ...

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        """关闭会话事件订阅。"""
        ...


__all__ = ["EventPort"]
