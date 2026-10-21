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
        self._event_pumps: dict[asyncio.Queue[EventResponse], tuple[asyncio.Queue[ApplicationEvent], asyncio.Task[None]]] = {}

    async def ensure_session(self, session_id: str) -> None:
        if await self._sessions.get(session_id) is None:
            raise EventSessionNotFoundError(session_id)

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[EventResponse], int]:
        await self.ensure_session(session_id)
        queue, watermark = await self._events.open_subscription(session_id)
        output: asyncio.Queue[EventResponse] = asyncio.Queue()
        async def pump() -> None:
            while True:
                await output.put(_event_response(await queue.get()))
        # Keep the transport-owned queue contract intact while exposing only
        # application response values to the HTTP layer.
        self._event_pumps[output] = (queue, asyncio.create_task(pump()))
        return output, watermark

    async def replay(
        self, session_id: str, after: int, upto: int
    ) -> list[EventResponse]:
        return [
            _event_response(value)
            for value in await self._events.list_events_between(session_id, after, upto)
        ]

    async def close(
        self, session_id: str, queue: asyncio.Queue[EventResponse]
    ) -> None:
        original, pump = self._event_pumps.pop(queue, (None, None))
        if pump is not None:
            pump.cancel()
        if original is not None:
            await self._events.close_subscription(session_id, original)

def _event_response(value: ApplicationEvent) -> EventResponse:
    """将事件信封转换为 SSE 使用的应用响应。"""
    return EventResponse(
        event_type=str(value.event_type), durability=str(value.durability), session_id=value.session_id,
        session_seq=value.session_seq, run_id=value.run_id, message_id=value.message_id,
        attachment_id=value.attachment_id, stream_id=value.stream_id, stream_type=value.stream_type,
        chunk_id=value.chunk_id, is_complete=value.is_complete, parent_run_id=value.parent_run_id,
        transition_id=value.transition_id, occurred_at=value.occurred_at, payload=dict(value.payload),
    )
