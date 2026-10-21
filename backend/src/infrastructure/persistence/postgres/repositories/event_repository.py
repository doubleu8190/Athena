from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.events import (
    ApplicationEvent,
    EventPort,
)

from ..models import (
    AgentEventModel,
)


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed

class PostgresEventRepository(
    EventPort,
):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory
        self._subscribers: dict[str, list[asyncio.Queue[ApplicationEvent]]] = {}

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        async with self._session_factory() as session:
            async with session.begin():
                max_seq = (
                    await session.scalar(
                        select(func.max(AgentEventModel.session_seq)).where(
                            AgentEventModel.session_id == event.session_id
                        )
                    )
                    or 0
                )
                value = ApplicationEvent(
                    event.event_type,
                    event.durability,
                    event.session_id,
                    int(max_seq) + 1,
                    event.run_id,
                    event.message_id,
                    event.attachment_id,
                    event.stream_id,
                    event.stream_type,
                    event.chunk_id,
                    event.is_complete,
                    event.parent_run_id,
                    event.transition_id,
                    event.occurred_at,
                    dict(event.payload),
                )
                session.add(
                    AgentEventModel(
                        session_id=value.session_id,
                        session_seq=value.session_seq,
                        run_id=value.run_id,
                        message_id=value.message_id,
                        attachment_id=value.attachment_id,
                        event_type=str(value.event_type),
                        durability=str(value.durability),
                        stream_id=value.stream_id,
                        stream_type=value.stream_type,
                        is_complete=int(value.is_complete),
                        parent_run_id=value.parent_run_id,
                        transition_id=value.transition_id,
                        payload_json=json.dumps(value.payload, ensure_ascii=False),
                        occurred_at=value.occurred_at.isoformat(),
                    )
                )
        for queue in self._subscribers.get(event.session_id, []):
            queue.put_nowait(value)
        return value

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        for queue in self._subscribers.get(event.session_id, []):
            queue.put_nowait(event)
        return event

    async def open_subscription(self, session_id: str):
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        async with self._session_factory() as session:
            watermark = (
                await session.scalar(
                    select(func.max(AgentEventModel.session_seq)).where(
                        AgentEventModel.session_id == session_id
                    )
                )
                or 0
            )
        return queue, int(watermark)

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None
    ):
        async with self._session_factory() as session:
            query = (
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.session_seq > after,
                )
                .order_by(AgentEventModel.session_seq)
            )
            if upto is not None:
                query = query.where(AgentEventModel.session_seq <= upto)
            rows = (await session.execute(query)).scalars().all()
        return [
            ApplicationEvent(
                row.event_type,
                row.durability,
                row.session_id,
                row.session_seq,
                row.run_id,
                row.message_id,
                row.attachment_id,
                row.stream_id,
                row.stream_type,
                None,
                bool(row.is_complete),
                row.parent_run_id,
                row.transition_id,
                datetime.fromisoformat(row.occurred_at),
                _json(row.payload_json, {}),
            )
            for row in rows
        ]

    async def list_events_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> tuple[list[ApplicationEvent], bool]:
        """按会话和运行游标读取链路事件，避免跨会话 run ID 越权。"""
        bounded_limit = min(max(limit, 1), 500)
        async with self._session_factory() as session:
            query = (
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.run_id == run_id,
                    AgentEventModel.session_seq > after,
                )
                .order_by(AgentEventModel.session_seq)
                .limit(bounded_limit + 1)
            )
            rows = (await session.execute(query)).scalars().all()
        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        return [
            ApplicationEvent(
                row.event_type,
                row.durability,
                row.session_id,
                row.session_seq,
                row.run_id,
                row.message_id,
                row.attachment_id,
                row.stream_id,
                row.stream_type,
                None,
                bool(row.is_complete),
                row.parent_run_id,
                row.transition_id,
                datetime.fromisoformat(row.occurred_at),
                _json(row.payload_json, {}),
            )
            for row in rows
        ], has_more

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        if queue in self._subscribers.get(session_id, []):
            self._subscribers[session_id].remove(queue)
