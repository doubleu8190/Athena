"""基于 Redis 通知与 PostgreSQL 对账的 Run SSE 服务。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Literal

from domain.auth import Authenticator
from domain.run.model import (
    RunEvent,
    RunHeartbeatEvent,
    RunHeartbeatPayload,
    RunInterruptedEvent,
    RunInterruptedPayload,
    RunPersistedEvent,
    RunPersistedEventPayload,
    RunSnapshotEvent,
    RunSnapshotPayload,
    RunStreamEvent,
    RunStreamSnapshot,
    RunTextDeltaEvent,
)
from domain.run.ports import (
    RunEventSubscription,
    RunEventTransport,
    RunEventTransportError,
    RunRepository,
)

logger = logging.getLogger(__name__)

STREAM_HEARTBEAT_SECONDS = 15.0
DATABASE_RECONCILE_SECONDS = 5.0
AUTH_RECHECK_SECONDS = 5.0
REDIS_RECONNECT_SECONDS = 2.0
STREAM_SEND_TIMEOUT_SECONDS = 1.0
MAX_STREAM_EVENT_BYTES = 64 * 1024


class RunStreamService:
    """只信任经 owner 查询和租约复核的 DB 状态，Redis 只负责唤醒。"""

    def __init__(
        self,
        repository: RunRepository,
        authenticator: Authenticator,
        transport: RunEventTransport | None,
    ) -> None:
        self._repository = repository
        self._authenticator = authenticator
        self._transport = transport

    async def authorize(self, session_id: str, owner_id: str, run_id: str) -> bool:
        """在返回 SSE 响应前执行 owner 与 soft-delete 检查。"""
        return await self._repository.get(session_id, owner_id, run_id) is not None

    async def events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        token: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncGenerator[RunStreamEvent, None]:
        """先订阅再读一致快照，之后消费 Pub/Sub 并周期性对账。"""
        exit_stack = AsyncExitStack()
        subscription: RunEventSubscription | None = None
        next_reconnect = 0.0
        last_auth_check = asyncio.get_running_loop().time()
        last_reconcile = last_auth_check
        last_heartbeat = last_auth_check
        output_sequence = 0
        event_cursor = 0
        transport_cursor: int | None = None
        deltas_interrupted = False
        current_epoch = 0
        current_status = None
        seen_message_ids: set[str] = set()
        try:
            subscription = await self._connect(exit_stack, run_id)
            if subscription is None and self._transport is not None:
                deltas_interrupted = True
                next_reconnect = asyncio.get_running_loop().time() + REDIS_RECONNECT_SECONDS
                yield self._interrupted(run_id, 0, output_sequence, "redis_unavailable")
                output_sequence += 1
            elif last_event_id:
                deltas_interrupted = True
                yield self._interrupted(run_id, 0, output_sequence, "redis_reconnected")
                output_sequence += 1

            snapshot = await self._snapshot(session_id, owner_id, run_id)
            if snapshot is None:
                return
            current_epoch = snapshot.run.execution_epoch
            transport_epoch = current_epoch
            deltas_interrupted = deltas_interrupted or bool(last_event_id)
            current_status = snapshot.run.status
            event_cursor = snapshot.events[-1].sequence if snapshot.events else 0
            seen_message_ids.update(message.id for message in snapshot.messages)
            yield RunSnapshotEvent(
                run_id=run_id,
                execution_epoch=current_epoch,
                stream_sequence=output_sequence,
                payload=RunSnapshotPayload(
                    status=snapshot.run.status,
                    event_sequence=snapshot.run.event_sequence,
                    report_message_id=snapshot.run.report_message_id,
                    messages=snapshot.messages,
                    events=snapshot.events,
                    messages_complete=snapshot.messages_complete,
                    messages_before_sequence=snapshot.messages_before_sequence,
                    events_complete=snapshot.events_complete,
                    events_after_sequence=snapshot.events_after_sequence,
                ),
            )
            output_sequence += 1
            initial_events = await self._events_through(
                session_id,
                owner_id,
                run_id,
                event_cursor,
                snapshot.run.event_sequence,
            )
            for event in initial_events:
                yield RunPersistedEvent(
                    run_id=run_id,
                    execution_epoch=current_epoch,
                    stream_sequence=output_sequence,
                    payload=RunPersistedEventPayload(event=event),
                )
                output_sequence += 1
                event_cursor = event.sequence
            if snapshot.run.status.value in {
                "waiting_for_input",
                "waiting_for_approval",
                "completed",
                "failed",
                "cancelled",
            }:
                return
            loop = asyncio.get_running_loop()

            while True:
                now = loop.time()
                if now - last_auth_check >= AUTH_RECHECK_SECONDS:
                    auth = await self._authenticator.authenticate(token)
                    if auth is None or auth.user.id != owner_id:
                        return
                    if await self._repository.get(session_id, owner_id, run_id) is None:
                        return
                    last_auth_check = now

                if subscription is None and self._transport is not None and now >= next_reconnect:
                    subscription = await self._connect(exit_stack, run_id)
                    next_reconnect = now + REDIS_RECONNECT_SECONDS
                    if subscription is not None:
                        deltas_interrupted = True
                        yield self._interrupted(
                            run_id, current_epoch, output_sequence, "redis_reconnected"
                        )
                        output_sequence += 1
                        last_reconcile = 0.0
                if subscription is not None:
                    try:
                        incoming = await subscription.get(timeout=0.5)
                    except RunEventTransportError as error:
                        # 传输故障只影响实时性：断开订阅并发送中断标记，之后靠数据库对账补齐。
                        logger.warning(
                            "run_stream_subscriber_failed exception_type=%s",
                            type(error).__name__,
                        )
                        await exit_stack.aclose()
                        exit_stack = AsyncExitStack()
                        subscription = None
                        deltas_interrupted = True
                        next_reconnect = now + REDIS_RECONNECT_SECONDS
                        yield self._interrupted(
                            run_id, current_epoch, output_sequence, "redis_unavailable"
                        )
                        output_sequence += 1
                        incoming = None
                else:
                    await asyncio.sleep(0.25)
                    incoming = None

                if isinstance(incoming, RunInterruptedEvent):
                    if incoming.run_id != run_id or not await self._repository.is_live_execution(
                        run_id, incoming.execution_epoch
                    ):
                        incoming = None
                    elif incoming.execution_epoch > current_epoch:
                        current_epoch = incoming.execution_epoch
                        transport_epoch = current_epoch
                        transport_cursor = None
                        deltas_interrupted = False
                    if incoming is not None:
                        if incoming.execution_epoch < current_epoch:
                            incoming = None
                    if incoming is not None and incoming.run_id == run_id:
                        deltas_interrupted = True
                        transport_cursor, gap = self._advance_transport_cursor(
                            incoming.stream_sequence, transport_cursor
                        )
                        if gap:
                            yield self._interrupted(
                                run_id, current_epoch, output_sequence, "stream_gap"
                            )
                            output_sequence += 1
                        yield incoming.model_copy(update={"stream_sequence": output_sequence})
                        output_sequence += 1
                elif isinstance(incoming, RunTextDeltaEvent):
                    is_live = await self._repository.is_live_execution(
                        run_id, incoming.execution_epoch
                    )
                    if incoming.run_id == run_id and is_live:
                        if incoming.execution_epoch > current_epoch:
                            current_epoch = incoming.execution_epoch
                            transport_epoch = current_epoch
                            transport_cursor = None
                            deltas_interrupted = False
                        if transport_epoch != incoming.execution_epoch:
                            incoming = None
                        if incoming is not None:
                            transport_cursor, gap = self._advance_transport_cursor(
                                incoming.stream_sequence, transport_cursor
                            )
                            if gap:
                                deltas_interrupted = True
                                yield self._interrupted(
                                    run_id, current_epoch, output_sequence, "stream_gap"
                                )
                                output_sequence += 1
                    if (
                        isinstance(incoming, RunTextDeltaEvent)
                        and incoming.run_id == run_id
                        and is_live
                        and not deltas_interrupted
                    ):
                        current_epoch = incoming.execution_epoch
                        yield incoming.model_copy(update={"stream_sequence": output_sequence})
                        output_sequence += 1
                elif isinstance(incoming, RunPersistedEvent):
                    if incoming.execution_epoch == transport_epoch:
                        transport_cursor, gap = self._advance_transport_cursor(
                            incoming.stream_sequence, transport_cursor
                        )
                        if gap:
                            deltas_interrupted = True
                            yield self._interrupted(
                                run_id, current_epoch, output_sequence, "stream_gap"
                            )
                            output_sequence += 1
                    if incoming.run_id == run_id:
                        # 正式通知只负责触发 DB 对账，状态快照必须先于本地状态更新发出。
                        last_reconcile = 0.0

                now = loop.time()
                if now - last_reconcile >= DATABASE_RECONCILE_SECONDS:
                    snapshot = await self._snapshot(session_id, owner_id, run_id)
                    if snapshot is None:
                        return
                    pending_events = await self._events_through(
                        session_id,
                        owner_id,
                        run_id,
                        event_cursor,
                        snapshot.run.event_sequence,
                    )
                    for event in pending_events:
                        yield RunPersistedEvent(
                            run_id=run_id,
                            execution_epoch=snapshot.run.execution_epoch,
                            stream_sequence=output_sequence,
                            payload=RunPersistedEventPayload(event=event),
                        )
                        output_sequence += 1
                        event_cursor = event.sequence
                    if (
                        snapshot.run.execution_epoch != current_epoch
                        or snapshot.run.status != current_status
                        or any(message.id not in seen_message_ids for message in snapshot.messages)
                    ):
                        seen_message_ids.update(message.id for message in snapshot.messages)
                        # 状态切换先发送权威快照，再更新本地状态，避免丢掉终态投影。
                        yield RunSnapshotEvent(
                            run_id=run_id,
                            execution_epoch=snapshot.run.execution_epoch,
                            stream_sequence=output_sequence,
                            payload=RunSnapshotPayload(
                                status=snapshot.run.status,
                                event_sequence=snapshot.run.event_sequence,
                                report_message_id=snapshot.run.report_message_id,
                                messages=snapshot.messages,
                                events=snapshot.events,
                                messages_complete=snapshot.messages_complete,
                                messages_before_sequence=snapshot.messages_before_sequence,
                                events_complete=snapshot.events_complete,
                                events_after_sequence=snapshot.events_after_sequence,
                            ),
                        )
                        output_sequence += 1
                        if snapshot.run.execution_epoch != current_epoch:
                            transport_epoch = snapshot.run.execution_epoch
                            transport_cursor = None
                            deltas_interrupted = False
                        current_epoch = snapshot.run.execution_epoch
                        current_status = snapshot.run.status
                    last_reconcile = now
                    if snapshot.run.status.value in {
                        "waiting_for_input",
                        "waiting_for_approval",
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        return

                if now - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                    yield RunHeartbeatEvent(
                        run_id=run_id,
                        execution_epoch=current_epoch,
                        stream_sequence=output_sequence,
                        payload=RunHeartbeatPayload(at=datetime.now(UTC)),
                    )
                    output_sequence += 1
                    last_heartbeat = now
        finally:
            await exit_stack.aclose()

    async def _connect(
        self, exit_stack: AsyncExitStack, run_id: str
    ) -> RunEventSubscription | None:
        if self._transport is None:
            return None
        try:
            return await exit_stack.enter_async_context(self._transport.subscribe(run_id))
        except (RunEventTransportError, TimeoutError) as error:
            # 连接失败时降级为无实时订阅；数据库快照仍能提供权威状态。
            logger.warning("run_stream_connect_failed exception_type=%s", type(error).__name__)
            return None

    async def _snapshot(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunStreamSnapshot | None:
        return await self._repository.get_stream_snapshot(session_id, owner_id, run_id)

    async def _events_through(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        after_sequence: int,
        through_sequence: int,
    ) -> tuple[RunEvent, ...]:
        """按固定数据库游标分页补齐超过首屏的 Run 状态事件。"""
        pending: list[RunEvent] = []
        cursor = after_sequence
        while cursor < through_sequence:
            page = await self._repository.list_events(
                session_id,
                owner_id,
                run_id,
                after_sequence=cursor,
                through_sequence=through_sequence,
                limit=100,
            )
            if not page:
                break
            pending.extend(page)
            cursor = page[-1].sequence
        return tuple(pending)

    @staticmethod
    def _advance_transport_cursor(sequence: int, cursor: int | None) -> tuple[int, bool]:
        """检测订阅期间丢失的 Pub/Sub 序号，防止继续展示不完整文本。"""
        return sequence, cursor is not None and sequence > cursor + 1

    @staticmethod
    def _interrupted(
        run_id: str,
        epoch: int,
        sequence: int,
        reason: Literal["redis_reconnected", "redis_unavailable", "stream_gap"],
    ) -> RunInterruptedEvent:
        return RunInterruptedEvent(
            run_id=run_id,
            execution_epoch=epoch,
            stream_sequence=sequence,
            payload=RunInterruptedPayload(reason=reason),
        )


__all__ = ["RunStreamService"]
