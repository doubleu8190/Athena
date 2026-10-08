"""会话事件历史重放和实时订阅用例。"""

from __future__ import annotations

import asyncio

from domain.events import (
    ApplicationEvent,
    EventHistoryPort,
    EventSubscriptionPort,
    RealtimeTransportPort,
)
from domain.sessions import SessionRepository


class EventSessionNotFoundError(LookupError):
    """事件订阅目标会话不存在。"""


class EventStreamService:
    """组合会话权限、事件 watermark 和订阅生命周期。"""

    def __init__(
        self,
        sessions: SessionRepository,
        subscriptions: EventSubscriptionPort,
        history: EventHistoryPort,
        transport: RealtimeTransportPort,
    ) -> None:
        self._sessions = sessions
        self._subscriptions = subscriptions
        self._history = history
        self._transport = transport

    async def ensure_session(self, session_id: str) -> None:
        if await self._sessions.get(session_id) is None:
            raise EventSessionNotFoundError(session_id)

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        await self.ensure_session(session_id)
        return await self._subscriptions.open_subscription(session_id)

    async def open(
        self, session_id: str, after: int | None = None
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """Compatibility alias; replay cursors belong to ``replay``."""
        return await self.open_subscription(session_id)

    async def replay(
        self, session_id: str, after: int, upto: int
    ) -> list[ApplicationEvent]:
        return await self._history.list_events_between(session_id, after, upto)

    async def close(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        await self._transport.close_subscription(session_id, queue)
