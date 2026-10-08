"""事件端口的兼容导出。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .entities import ApplicationEvent
from .publisher_ports import EventPublisherPort
from .store_ports import EventHistoryPort
from .subscription_ports import EventSubscriptionPort


class EventStorePort(EventPublisherPort, EventHistoryPort, EventSubscriptionPort, Protocol):
    """兼容旧调用方的事件组合端口。"""


class RealtimeTransportPort(Protocol):
    async def close_subscription(self, session_id: str, queue: asyncio.Queue[ApplicationEvent]) -> None: ...


__all__ = ["EventHistoryPort", "EventPublisherPort", "EventStorePort", "EventSubscriptionPort", "RealtimeTransportPort"]
