from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.events import (
    ApplicationEvent,
    EventPort,
)
from domain.runs import RunStatus
from domain.leases import LeaseGrant, LeaseKey

from ..models import (
    AgentEventModel,
    AgentRunModel,
    OrchestrationOutboxModel,
)
from .lease_center import PostgresLeaseCenter

_SUBSCRIBER_QUEUE_SIZE = 256
_EXECUTION_OUTPUT_EVENTS = frozenset({
    "message.started", "message.delta", "message.completed", "stream.snapshot",
    "thinking.started", "thinking.summary", "thinking.completed",
})


def _event_model(value: ApplicationEvent) -> AgentEventModel:
    """将事件信封转换为 ORM 行。"""
    return AgentEventModel(
        session_id=value.session_id,
        session_seq=value.session_seq,
        run_id=value.run_id,
        message_id=value.message_id,
        attachment_id=value.attachment_id,
        event_type=str(value.event_type),
        durability=str(value.durability),
        stream_id=value.stream_id,
        stream_type=value.stream_type,
        is_complete=value.is_complete,
        parent_run_id=value.parent_run_id,
        transition_id=value.transition_id,
        payload_json=dict(value.payload),
        occurred_at=value.occurred_at,
    )


def _event_from_model(row: AgentEventModel) -> ApplicationEvent:
    """还原已持久化事件，供幂等重放和 SSE 广播共用。"""
    payload = dict(row.payload_json)
    return ApplicationEvent(
        row.event_type,
        row.durability,
        row.session_id,
        row.session_seq,
        row.run_id,
        row.message_id,
        row.attachment_id,
        row.stream_id,
        row.stream_type,
        (
            payload.get("chunk_id", payload.get("last_chunk_id"))
            if isinstance(payload, dict)
            else None
        ),
        row.is_complete,
        row.parent_run_id,
        row.transition_id,
        row.occurred_at,
        payload,
    )


def _ensure_same_transition(
    existing: ApplicationEvent, requested: ApplicationEvent
) -> None:
    """拒绝用同一事件身份提交不同业务内容。"""
    if (
        str(existing.event_type) != str(requested.event_type)
        or str(existing.durability) != str(requested.durability)
        or existing.run_id != requested.run_id
        or existing.message_id != requested.message_id
        or existing.stream_id != requested.stream_id
        or existing.stream_type != requested.stream_type
        or existing.chunk_id != requested.chunk_id
        or existing.is_complete != requested.is_complete
        or existing.payload != requested.payload
    ):
        raise ValueError("transition_id_reused_with_different_event")


class PostgresEventRepository(
    EventPort,
):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory
        self._leases = PostgresLeaseCenter(session_factory)
        self._subscribers: dict[str, list[asyncio.Queue[ApplicationEvent]]] = {}

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        if str(event.event_type) in _EXECUTION_OUTPUT_EVENTS:
            raise ValueError("execution_event_requires_lease")
        return await self._publish(event, lease=None)

    async def publish_with_lease(
        self, event: ApplicationEvent, *, lease: LeaseGrant
    ) -> ApplicationEvent:
        """以当前 Root 或 Outbox grant 保护持久事件写入。"""
        if lease.key.resource_type == "root_execution":
            if not event.run_id or lease.key != LeaseKey("root_execution", event.run_id):
                raise ValueError("root_execution_lease_mismatch")
        elif lease.key.resource_type == "orchestration_outbox":
            if event.transition_id != f"orchestration-outbox:{lease.key.resource_id}":
                raise ValueError("orchestration_outbox_lease_mismatch")
        else:
            raise ValueError("event_lease_resource_invalid")
        return await self._publish(event, lease=lease)

    async def _publish(
        self, event: ApplicationEvent, *, lease: LeaseGrant | None
    ) -> ApplicationEvent:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    event_type = str(event.event_type)
                    run = None
                    output_start_events = _EXECUTION_OUTPUT_EVENTS - {"message.completed"}
                    if event_type in _EXECUTION_OUTPUT_EVENTS:
                        run = await session.scalar(
                            select(AgentRunModel)
                            .where(AgentRunModel.run_id == event.run_id)
                            .with_for_update()
                        )
                        allowed_statuses = (
                            {RunStatus.RUNNING.value}
                            if event_type in output_start_events
                            else {RunStatus.RUNNING.value, RunStatus.COMPLETED.value}
                        )
                        if (
                            run is None
                            or run.session_id != event.session_id
                            or run.status not in allowed_statuses
                        ):
                            raise ValueError("answer_stream_run_not_running")

                    if lease is not None:
                        if lease.key.resource_type == "orchestration_outbox":
                            if run is None:
                                run = await session.get(
                                    AgentRunModel, event.run_id, with_for_update=True
                                )
                            if run is None or run.session_id != event.session_id:
                                raise ValueError("orchestration_outbox_session_mismatch")
                            outbox = await session.get(
                                OrchestrationOutboxModel, lease.key.resource_id,
                                with_for_update=True,
                            )
                            if (outbox is None or outbox.status != "pending"
                                    or outbox.correlation_id != event.run_id):
                                raise ValueError("orchestration_outbox_event_mismatch")
                        await session.scalar(
                            select(func.pg_advisory_xact_lock(
                                func.hashtextextended(event.session_id, 0)
                            ))
                        )
                        await self._leases.assert_current_in_transaction(session, lease)

                    existing = await self._get_transition_event(session, event)
                    if existing is not None:
                        value = _event_from_model(existing)
                        _ensure_same_transition(value, event)
                    else:
                        # MAX(session_seq) + 1 必须按会话串行化；否则并发发布
                        # 会同时读取相同最大值，并争用同一个主键。
                        if lease is None:
                            await session.scalar(
                                select(func.pg_advisory_xact_lock(
                                    func.hashtextextended(event.session_id, 0)
                                ))
                            )
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
                        session.add(_event_model(value))
                        if value.transition_id is not None:
                            await session.flush()
        except IntegrityError:
            if lease is not None:
                raise
            if event.transition_id is None:
                raise
            async with self._session_factory() as session:
                existing = await self._get_transition_event(session, event)
            if existing is None:
                raise
            value = _event_from_model(existing)
            _ensure_same_transition(value, event)
        for queue in self._subscribers.get(event.session_id, []):
            try:
                queue.put_nowait(value)
            except asyncio.QueueFull:
                # Durable 事件已在数据库中，SSE 由任意唤醒或心跳补读；
                # realtime 事件允许在拥塞时丢弃，避免无界堆积。
                continue
        return value

    async def _get_transition_event(
        self, session: AsyncSession, event: ApplicationEvent
    ) -> AgentEventModel | None:
        if event.transition_id is None:
            return None
        return (
            await session.execute(
                select(AgentEventModel).where(
                    AgentEventModel.session_id == event.session_id,
                    AgentEventModel.transition_id == event.transition_id,
                )
            )
        ).scalar_one_or_none()

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        for queue in self._subscribers.get(event.session_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue
        return event

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """注册实时唤醒队列并读取注册期间的持久事件水位。

        参数：``session_id`` 是事件所属会话。
        返回：订阅队列和注册时的最大会话序号。
        异常：读取水位失败或任务取消时移除临时订阅后继续抛出。
        """
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        self._subscribers.setdefault(session_id, []).append(queue)
        try:
            async with self._session_factory() as session:
                watermark = (
                    await session.scalar(
                        select(func.max(AgentEventModel.session_seq)).where(
                            AgentEventModel.session_id == session_id
                        )
                    )
                    or 0
                )
        except BaseException:
            subscribers = self._subscribers.get(session_id)
            if subscribers is not None and queue in subscribers:
                subscribers.remove(queue)
                if not subscribers:
                    self._subscribers.pop(session_id, None)
            raise
        return queue, int(watermark)

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None,
        limit: int = 256,
    ):
        bounded_limit = min(max(limit, 1), 500)
        async with self._session_factory() as session:
            hidden_stopped_output = (
                select(AgentRunModel.run_id)
                .where(
                    AgentRunModel.run_id == AgentEventModel.run_id,
                    AgentRunModel.display_cutoff_session_seq.is_not(None),
                    AgentEventModel.session_seq > AgentRunModel.display_cutoff_session_seq,
                    AgentEventModel.event_type.in_(
                        (
                            "message.started",
                            "message.delta",
                            "message.completed",
                            "stream.snapshot",
                            "thinking.started",
                            "thinking.summary",
                            "thinking.completed",
                        )
                    ),
                )
                .exists()
            )
            query = (
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.session_seq > after,
                    ~hidden_stopped_output,
                )
                .order_by(AgentEventModel.session_seq)
            )
            if upto is not None:
                query = query.where(AgentEventModel.session_seq <= upto)
            query = query.limit(bounded_limit)
            rows = (await session.execute(query)).scalars().all()
        return [_event_from_model(row) for row in rows]

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
            hidden_stopped_output = (
                select(AgentRunModel.run_id)
                .where(
                    AgentRunModel.run_id == AgentEventModel.run_id,
                    AgentRunModel.display_cutoff_session_seq.is_not(None),
                    AgentEventModel.session_seq > AgentRunModel.display_cutoff_session_seq,
                    AgentEventModel.event_type.in_(
                        (
                            "message.started",
                            "message.delta",
                            "message.completed",
                            "stream.snapshot",
                            "thinking.started",
                            "thinking.summary",
                            "thinking.completed",
                        )
                    ),
                )
                .exists()
            )
            query = (
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.run_id == run_id,
                    AgentEventModel.session_seq > after,
                    ~hidden_stopped_output,
                )
                .order_by(AgentEventModel.session_seq)
                .limit(bounded_limit + 1)
            )
            rows = (await session.execute(query)).scalars().all()
        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        return [_event_from_model(row) for row in rows], has_more

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        if queue in self._subscribers.get(session_id, []):
            self._subscribers[session_id].remove(queue)
