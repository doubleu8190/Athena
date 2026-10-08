"""事件发布端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import ApplicationEvent


class EventPublisherPort(Protocol):
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent: ...
    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent: ...


__all__ = ["EventPublisherPort"]
