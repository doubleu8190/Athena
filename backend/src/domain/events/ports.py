"""事件持久化、发布和实时传输端口。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .entities import ApplicationEvent


class EventStorePort(Protocol):
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent: ...

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]: ...

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None
    ) -> list[ApplicationEvent]: ...


class EventPublisherPort(Protocol):
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent: ...

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent: ...


class RealtimeTransportPort(Protocol):
    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None: ...

