"""会话事件历史重放和实时订阅用例。"""

from __future__ import annotations

import asyncio

from domain.events import ApplicationEvent, EventPort
from domain.sessions import SessionRepository
from application.common.responses import EventResponse


class EventSessionNotFoundError(LookupError):
    """事件订阅目标会话不存在。"""


class EventStreamService:
    """组合会话权限、事件 watermark 和订阅生命周期。"""

    def __init__(
        self,
        sessions: SessionRepository,
        events: EventPort,
    ) -> None:
        self._sessions = sessions
        self._events = events

    async def ensure_session(self, session_id: str) -> None:
        if await self._sessions.get(session_id) is None:
            raise EventSessionNotFoundError(session_id)

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        await self.ensure_session(session_id)
        return await self._events.open_subscription(session_id)

    async def replay(
        self, session_id: str, after: int, upto: int | None, limit: int = 256
    ) -> list[EventResponse]:
        return [
            _event_response(value)
            for value in await self._events.list_events_between(
                session_id, after, upto, limit=limit
            )
        ]

    @staticmethod
    def response(value: ApplicationEvent) -> EventResponse:
        return _event_response(value)

    async def close(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        await self._events.close_subscription(session_id, queue)

def _event_response(value: ApplicationEvent) -> EventResponse:
    """将事件信封转换为 SSE 使用的应用响应。"""
    return EventResponse(
        event_type=str(value.event_type), durability=str(value.durability), session_id=value.session_id,
        session_seq=value.session_seq, run_id=value.run_id, message_id=value.message_id,
        attachment_id=value.attachment_id, stream_id=value.stream_id, stream_type=value.stream_type,
        chunk_id=value.chunk_id, is_complete=value.is_complete, parent_run_id=value.parent_run_id,
        transition_id=value.transition_id, occurred_at=value.occurred_at, payload=dict(value.payload),
    )
