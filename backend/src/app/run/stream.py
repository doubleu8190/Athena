"""基于 Redis 实时通知与 PostgreSQL 权威对账的 Run SSE 服务。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Literal

from domain.auth import Authenticator
from domain.run.model import (
    RunCompletedPayload,
    RunEvent,
    RunHeartbeatEvent,
    RunHeartbeatPayload,
    RunInterruptedEvent,
    RunInterruptedPayload,
    RunPersistedEvent,
    RunPersistedEventPayload,
    RunSnapshotEvent,
    RunSnapshotPayload,
    RunStatus,
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

# 收到终态或人工等待态权威快照后立即关闭 SSE 连接，不拉长连接等待异步收尾。
_TERMINAL_OR_WAITING_STATUSES = frozenset(
    {
        RunStatus.WAITING_FOR_INPUT,
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)

# 仅推进执行进度、不改变 Run 状态/正式消息/审批卡片/子智能体投影的纯进展型事件；
# 经 Redis 连续到达时直接转发并推进本地游标，免去每次触发 REPEATABLE READ 全量快照查库。
_DIRECT_FORWARD_EVENT_TYPES = frozenset(
    {
        "tool_call_started",
        "tool_call_finished",
        "compression_started",
        "compression_completed",
        "compression_failed",
    }
)


class RunStreamService:
    """结合 Redis 实时推送与 PostgreSQL 权威快照对账的 Run SSE 编排服务。"""

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
        """先订阅再读一致快照，随后按权威 epoch 与连续进度直传并辅以周期对账。"""
        exit_stack = AsyncExitStack()
        loop = asyncio.get_running_loop()
        next_reconnect = 0.0
        last_auth_check = last_reconcile = last_heartbeat = loop.time()
        output_sequence = 0
        transport_cursor: int | None = None
        deltas_interrupted = False

        def next_seq() -> int:
            nonlocal output_sequence
            seq = output_sequence
            output_sequence += 1
            return seq

        try:
            subscription = await self._connect(exit_stack, run_id)
            if subscription is None and self._transport is not None:
                deltas_interrupted = True
                next_reconnect = loop.time() + REDIS_RECONNECT_SECONDS
                yield self._interrupted(run_id, 0, next_seq(), "redis_unavailable")
            elif last_event_id:
                deltas_interrupted = True
                yield self._interrupted(run_id, 0, next_seq(), "redis_reconnected")

            snapshot = await self._repository.get_stream_snapshot(session_id, owner_id, run_id)
            if snapshot is None:
                return
            last_snapshot = snapshot
            current_epoch = snapshot.run.execution_epoch
            event_cursor = snapshot.events[-1].sequence if snapshot.events else 0
            seen_message_ids = {message.id for message in snapshot.messages}
            yield self._snapshot_event(run_id, next_seq(), snapshot)

            for event in await self._events_through(
                session_id, owner_id, run_id, event_cursor, snapshot.run.event_sequence
            ):
                yield self._persisted(run_id, current_epoch, next_seq(), event)
                event_cursor = event.sequence
            if snapshot.run.status in _TERMINAL_OR_WAITING_STATUSES:
                return

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
                            run_id, current_epoch, next_seq(), "redis_reconnected"
                        )
                        last_reconcile = 0.0

                if subscription is not None:
                    try:
                        incoming = await subscription.get(timeout=0.5)
                    except RunEventTransportError as error:
                        # 传输故障只影响实时性：断开订阅并发送中断标记，之后靠数据库对账补齐。
                        logger.warning(
                            "run_stream_subscriber_failed exception_type=%s", type(error).__name__
                        )
                        await exit_stack.aclose()
                        exit_stack = AsyncExitStack()
                        subscription = None
                        deltas_interrupted = True
                        next_reconnect = now + REDIS_RECONNECT_SECONDS
                        yield self._interrupted(
                            run_id, current_epoch, next_seq(), "redis_unavailable"
                        )
                        incoming = None
                else:
                    await asyncio.sleep(0.25)
                    incoming = None

                if incoming is not None and incoming.run_id == run_id:
                    # 第一步：按权威 execution_epoch 过滤实时增量与中断，不再以 1s TTL 高频查库；
                    # 仅观察到更高执行代次跃升时向数据库复核一次存活状态。
                    should_handle = isinstance(incoming, RunPersistedEvent)
                    if isinstance(incoming, (RunInterruptedEvent, RunTextDeltaEvent)):
                        should_handle = (
                            incoming.execution_epoch == current_epoch
                            and current_epoch > 0
                            and last_snapshot.run.status is not RunStatus.CANCELLING
                        )
                        if (
                            not should_handle
                            and incoming.execution_epoch > current_epoch
                            and incoming.execution_epoch > 0
                            and await self._repository.is_live_execution(
                                run_id, incoming.execution_epoch
                            )
                        ):
                            current_epoch = incoming.execution_epoch
                            transport_cursor = None
                            deltas_interrupted = False
                            should_handle = True

                    # 第二步：对同代次到达的事件统一检测 Pub/Sub 序号缺口。
                    if should_handle and incoming.execution_epoch == current_epoch:
                        if (
                            transport_cursor is not None
                            and incoming.stream_sequence > transport_cursor + 1
                        ):
                            deltas_interrupted = True
                            yield self._interrupted(run_id, current_epoch, next_seq(), "stream_gap")
                        transport_cursor = incoming.stream_sequence

                    # 第三步：分发中断、文本增量或持久化事件。
                    if should_handle and isinstance(incoming, RunInterruptedEvent):
                        deltas_interrupted = True
                        yield incoming.model_copy(update={"stream_sequence": next_seq()})
                    elif (
                        should_handle
                        and isinstance(incoming, RunTextDeltaEvent)
                        and not deltas_interrupted
                    ):
                        yield incoming.model_copy(update={"stream_sequence": next_seq()})
                    elif isinstance(incoming, RunPersistedEvent):
                        persisted = incoming.payload.event
                        is_child = persisted.run_id != run_id
                        if (
                            not is_child
                            and incoming.execution_epoch == current_epoch
                            and current_epoch > 0
                            and last_snapshot.run.status is RunStatus.RUNNING
                            and persisted.sequence == event_cursor + 1
                            and persisted.type in _DIRECT_FORWARD_EVENT_TYPES
                        ):
                            # 序号连续的纯进展型非终态事件直接转发并推进游标，免去全量快照查库。
                            yield self._persisted(run_id, current_epoch, next_seq(), persisted)
                            event_cursor = persisted.sequence
                        elif (
                            is_child
                            or persisted.sequence > event_cursor
                            or incoming.execution_epoch != current_epoch
                        ):
                            # 终态/等待态/子智能体变更、序号缺口或代次切换时立即触发 DB 权威对账。
                            last_reconcile = 0.0

                now = loop.time()
                if now - last_reconcile >= DATABASE_RECONCILE_SECONDS:
                    snapshot = await self._repository.get_stream_snapshot(
                        session_id, owner_id, run_id
                    )
                    if snapshot is None:
                        return
                    for event in await self._events_through(
                        session_id, owner_id, run_id, event_cursor, snapshot.run.event_sequence
                    ):
                        yield self._persisted(
                            run_id, snapshot.run.execution_epoch, next_seq(), event
                        )
                        event_cursor = event.sequence
                    if self._snapshot_changed(last_snapshot, snapshot, seen_message_ids):
                        seen_message_ids.update(message.id for message in snapshot.messages)
                        # 状态或消息投影变化时先推送权威快照，再更新本地状态。
                        yield self._snapshot_event(run_id, next_seq(), snapshot)
                        if snapshot.run.execution_epoch != current_epoch:
                            transport_cursor = None
                            deltas_interrupted = False
                        current_epoch = snapshot.run.execution_epoch
                        last_snapshot = snapshot
                    last_reconcile = now
                    if snapshot.run.status in _TERMINAL_OR_WAITING_STATUSES:
                        return

                if now - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                    yield RunHeartbeatEvent(
                        run_id=run_id,
                        execution_epoch=current_epoch,
                        stream_sequence=next_seq(),
                        payload=RunHeartbeatPayload(at=datetime.now(UTC)),
                    )
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
            logger.warning("run_stream_connect_failed exception_type=%s", type(error).__name__)
            return None

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
    def _snapshot_changed(
        previous: RunStreamSnapshot,
        current: RunStreamSnapshot,
        seen_message_ids: set[str],
    ) -> bool:
        """与上次实际下发的权威快照对比，避免实时增量提前推进代次后漏发新代次快照。"""
        return (
            current.run.execution_epoch != previous.run.execution_epoch
            or current.run.status != previous.run.status
            or any(message.id not in seen_message_ids for message in current.messages)
            or current.subagent_tasks != previous.subagent_tasks
            or current.pending_subagent_inputs != previous.pending_subagent_inputs
            or current.pending_subagent_approvals != previous.pending_subagent_approvals
        )

    @staticmethod
    def _snapshot_event(
        run_id: str, sequence: int, snapshot: RunStreamSnapshot
    ) -> RunSnapshotEvent:
        """构造权威快照帧，并在 completed 态补齐对称的 assistant_message_id。"""
        assistant_message_id = snapshot.assistant_message_id
        if assistant_message_id is None and snapshot.run.status is RunStatus.COMPLETED:
            assistant_message_id = next(
                (
                    event.payload.assistant_message_id
                    for event in reversed(snapshot.events)
                    if isinstance(event.payload, RunCompletedPayload)
                ),
                None,
            ) or (
                next((m.id for m in reversed(snapshot.messages) if m.role == "assistant"), None)
                if snapshot.run.depth == 0
                else None
            )
        return RunSnapshotEvent(
            run_id=run_id,
            execution_epoch=snapshot.run.execution_epoch,
            stream_sequence=sequence,
            payload=RunSnapshotPayload(
                status=snapshot.run.status,
                event_sequence=snapshot.run.event_sequence,
                report_message_id=snapshot.run.report_message_id,
                assistant_message_id=assistant_message_id,
                messages=snapshot.messages,
                events=snapshot.events,
                messages_complete=snapshot.messages_complete,
                messages_before_sequence=snapshot.messages_before_sequence,
                events_complete=snapshot.events_complete,
                events_after_sequence=snapshot.events_after_sequence,
                subagent_tasks=snapshot.subagent_tasks,
                pending_subagent_inputs=snapshot.pending_subagent_inputs,
                pending_subagent_approvals=snapshot.pending_subagent_approvals,
            ),
        )

    @staticmethod
    def _persisted(run_id: str, epoch: int, sequence: int, event: RunEvent) -> RunPersistedEvent:
        return RunPersistedEvent(
            run_id=run_id,
            execution_epoch=epoch,
            stream_sequence=sequence,
            payload=RunPersistedEventPayload(event=event),
        )

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
