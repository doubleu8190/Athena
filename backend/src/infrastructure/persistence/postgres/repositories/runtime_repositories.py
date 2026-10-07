"""Native target repositories for runs, events, approvals, memory and retrieval."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from domain.common.query_ports import RetrievalQueryPort
from domain.events import ApplicationEvent
from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryWriteCommand,
)
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    CommandType,
    RunCommand,
    RunQueryPort,
    RunStatus,
    RunSummary,
)

from ..models import (
    AgentCommandModel,
    AgentEventModel,
    AgentRunModel,
    ApprovalRecordModel,
    MemoryModel,
    RetrievalRunModel,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed


class PostgresRunRepository(RunQueryPort):
    """Command queue, run projection and lifecycle in the target schema."""

    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def enqueue_command(self, command: RunCommand) -> CommandEnqueueResult:
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(AgentCommandModel, command.command_id)
                if existing is not None:
                    return self._result(existing, True)
                now = command.issued_at.isoformat()
                session.add(
                    AgentCommandModel(
                        command_id=command.command_id,
                        session_id=command.session_id,
                        run_id=command.run_id,
                        command_type=str(command.command_type),
                        schema_version=command.schema_version,
                        idempotency_key=command.command_id,
                        payload_json=json.dumps(
                            dict(command.payload), ensure_ascii=False
                        ),
                        status=CommandStatus.QUEUED.value,
                        queued_at=now,
                    )
                )
                if (
                    command.run_id
                    and await session.get(AgentRunModel, command.run_id) is None
                ):
                    session.add(
                        AgentRunModel(
                            run_id=command.run_id,
                            session_id=command.session_id,
                            status=RunStatus.CREATED.value,
                            request_json=json.dumps(dict(command.payload)),
                            root_thread_id=f"thread-{command.run_id}",
                            created_at=now,
                            updated_at=now,
                        )
                    )
            return CommandEnqueueResult(
                command.command_id,
                command.run_id,
                command.payload.get("message_id"),
                tuple(command.payload.get("attachment_ids", ())),
                CommandStatus.QUEUED,
                False,
            )

    async def get_command(self, command_id: str) -> CommandStatusRecord | None:
        async with self._session_factory() as session:
            row = await session.get(AgentCommandModel, command_id)
        return None if row is None else self._record(row)

    async def claim_next_command(self) -> RunCommand | None:
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentCommandModel)
                        .where(AgentCommandModel.status == CommandStatus.QUEUED.value)
                        .order_by(AgentCommandModel.queued_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                row.status = CommandStatus.RUNNING.value
                row.started_at = _now()
                return RunCommand(
                    row.command_id,
                    CommandType(row.command_type),
                    row.session_id,
                    row.run_id,
                    _json(row.payload_json, {}),
                    datetime.fromisoformat(row.queued_at),
                    row.schema_version,
                )

    async def complete_command(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(AgentCommandModel, command_id)
                if row is not None:
                    row.status = str(status)
                    row.result_json = (
                        json.dumps(result, default=str) if result is not None else None
                    )
                    row.error_json = (
                        json.dumps(error, default=str) if error is not None else None
                    )
                    row.completed_at = _now()

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(AgentRunModel.session_id == session_id)
                        .order_by(AgentRunModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return [self._run(row) for row in rows]

    async def get(self, run_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            row = await session.get(AgentRunModel, run_id)
        return None if row is None else self._run(row)

    async def get_active_for_session(self, session_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.session_id == session_id,
                        AgentRunModel.status.in_(
                            [
                                RunStatus.CREATED.value,
                                RunStatus.RUNNING.value,
                                RunStatus.PAUSED.value,
                            ]
                        ),
                    )
                    .order_by(AgentRunModel.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return None if row is None else self._run(row)

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(AgentRunModel)
                    .where(AgentRunModel.run_id == run_id)
                    .values(status=str(status), updated_at=_now())
                )
        return bool(result.rowcount)

    async def complete(self, run_id: str, result: dict[str, object]) -> bool:
        return await self._finish(run_id, RunStatus.COMPLETED, response=result)

    async def fail(self, run_id: str, error: dict[str, object]) -> bool:
        return await self._finish(run_id, RunStatus.FAILED, error=error)

    async def cancel(self, run_id: str) -> bool:
        return await self._finish(run_id, RunStatus.CANCELLED)

    async def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        response: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status.value, "updated_at": _now()}
        if response is not None:
            values["response_json"] = json.dumps(response, ensure_ascii=False)
        if error is not None:
            values["error_json"] = json.dumps(error, ensure_ascii=False)
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(AgentRunModel)
                    .where(AgentRunModel.run_id == run_id)
                    .values(**values)
                )
        return bool(result.rowcount)

    @staticmethod
    def _result(row: AgentCommandModel, deduplicated: bool) -> CommandEnqueueResult:
        payload = _json(row.payload_json, {})
        return CommandEnqueueResult(
            row.command_id,
            row.run_id,
            payload.get("message_id"),
            tuple(payload.get("attachment_ids", ())),
            row.status,
            deduplicated,
        )

    @staticmethod
    def _record(row: AgentCommandModel) -> CommandStatusRecord:
        return CommandStatusRecord(
            row.command_id,
            row.session_id,
            row.run_id,
            row.command_type,
            row.status,
            _json(row.result_json, None),
            _json(row.error_json, None),
        )

    @staticmethod
    def _run(row: AgentRunModel) -> RunSummary:
        return RunSummary(
            row.run_id,
            row.session_id,
            row.status,
            row.root_thread_id,
            _json(row.error_json, None),
            datetime.fromisoformat(row.created_at),
            datetime.fromisoformat(row.updated_at),
        )


class PostgresEventRepository:
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

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        if queue in self._subscribers.get(session_id, []):
            self._subscribers[session_id].remove(queue)


class PostgresApprovalRepository:
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def create(self, request: ApprovalRequest) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                if await session.get(ApprovalRecordModel, request.approval_id):
                    return False
                session.add(
                    ApprovalRecordModel(
                        approval_id=request.approval_id,
                        session_id=request.session_id,
                        run_id=request.run_id,
                        approval_batch_id=request.approval_batch_id,
                        plan_id=request.plan_id,
                        task_id=request.task_id,
                        tool_call_id=request.tool_call_id,
                        tool_name=request.tool_name,
                        arguments_json=json.dumps(request.arguments),
                        risk_level=request.risk_level,
                        status=request.status.value,
                        decision=request.decision.value if request.decision else None,
                        created_at=(
                            request.created_at or datetime.now(timezone.utc)
                        ).isoformat(),
                    )
                )
        return True

    async def get(self, approval_id: str):
        async with self._session_factory() as session:
            row = await session.get(ApprovalRecordModel, approval_id)
        return None if row is None else self._domain(row)

    async def list_pending(self, session_id: str | None = None):
        return await self._list(session_id, pending=True)

    async def list_history(
        self, session_id: str | None = None, *, limit: int = 50, offset: int = 0
    ):
        return await self._list(session_id, pending=False, limit=limit, offset=offset)

    async def _list(self, session_id, *, pending, limit=100000, offset=0):
        async with self._session_factory() as session:
            query = (
                select(ApprovalRecordModel)
                .where(
                    ApprovalRecordModel.status
                    == (
                        ApprovalStatus.PENDING.value
                        if pending
                        else ApprovalStatus.RESOLVED.value
                    )
                )
                .order_by(ApprovalRecordModel.created_at)
            )
            if session_id is not None:
                query = query.where(ApprovalRecordModel.session_id == session_id)
            rows = (
                (await session.execute(query.offset(offset).limit(limit)))
                .scalars()
                .all()
            )
        return [self._domain(row) for row in rows]

    async def resolve(
        self, approval_id: str, *, run_id: str, task_id: str, decision: ApprovalDecision
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(ApprovalRecordModel)
                    .where(
                        ApprovalRecordModel.approval_id == approval_id,
                        ApprovalRecordModel.run_id == run_id,
                        ApprovalRecordModel.status == ApprovalStatus.PENDING.value,
                    )
                    .values(
                        status=ApprovalStatus.RESOLVED.value,
                        decision=decision.value,
                        decided_at=_now(),
                    )
                )
        return bool(result.rowcount)

    async def resolve_batch(
        self,
        batch_id: str,
        decisions: dict[str, ApprovalDecision],
        *,
        run_id: str | None = None,
    ) -> bool:
        changed = False
        for approval_id, decision in decisions.items():
            row = await self.get(approval_id)
            if (
                row
                and row.approval_batch_id == batch_id
                and (run_id is None or row.run_id == run_id)
            ):
                changed = (
                    await self.resolve(
                        approval_id,
                        run_id=row.run_id,
                        task_id=row.task_id or "",
                        decision=decision,
                    )
                    or changed
                )
        return changed

    @staticmethod
    def _domain(row):
        return ApprovalRequest(
            row.approval_id,
            row.session_id,
            row.run_id,
            row.tool_call_id,
            row.tool_name,
            _json(row.arguments_json, {}),
            row.risk_level,
            row.task_id,
            row.plan_id,
            row.approval_batch_id,
            ApprovalStatus(row.status),
            ApprovalDecision(row.decision) if row.decision else None,
            datetime.fromisoformat(row.created_at),
            datetime.fromisoformat(row.decided_at) if row.decided_at else None,
        )


class PostgresMemoryRepository:
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def initialize(self) -> None:
        return None

    async def search(self, request: MemorySearchRequest):
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryModel)
                        .where(
                            MemoryModel.content.ilike(f"%{request.query}%"),
                            MemoryModel.status == MemoryStatus.ACTIVE.value,
                        )
                        .limit(request.limit)
                    )
                )
                .scalars()
                .all()
            )
        return [self._domain(row, score=1.0) for row in rows]

    async def list(self, request: MemoryListRequest):
        async with self._session_factory() as session:
            query = (
                select(MemoryModel)
                .where(MemoryModel.status != MemoryStatus.DELETED.value)
                .order_by(MemoryModel.created_at)
                .offset(request.offset)
                .limit(request.limit)
            )
            if request.session_id:
                query = query.where(MemoryModel.session_id == request.session_id)
            rows = (await session.execute(query)).scalars().all()
            total = (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryModel)
                    .where(MemoryModel.status != MemoryStatus.DELETED.value)
                )
                or 0
            )
        values = tuple(self._domain(row) for row in rows)
        return MemoryPage(
            values,
            int(total),
            {"total": int(total), "expired": 0, "recent": len(values)},
        )

    async def get(self, memory_id: str):
        async with self._session_factory() as session:
            row = await session.get(MemoryModel, memory_id)
        return None if row is None else self._domain(row)

    async def revisions(self, memory_id: str):
        value = await self.get(memory_id)
        return [] if value is None else [value]

    async def add(self, command: MemoryWriteCommand) -> str:
        key = (
            command.operation_key or f"memory-{datetime.now(timezone.utc).timestamp()}"
        )
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    MemoryModel(
                        id=key,
                        logical_memory_id=key,
                        session_id=str(command.metadata.get("session_id", "")),
                        content=command.content,
                        metadata_json=json.dumps(command.metadata),
                        created_at=_now(),
                        status=MemoryStatus.ACTIVE.value,
                        validity_status="valid",
                        revision=1,
                    )
                )
        return key

    async def revise(
        self, memory_id: str, content: str, *, metadata=None, operation_key=None
    ):
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(
                        content=content,
                        metadata_json=json.dumps(metadata or {}),
                        revision=MemoryModel.revision + 1,
                    )
                )
        return memory_id if result.rowcount else None

    async def set_validity(
        self, memory_id: str, validity_status: str, *, valid_until=None
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(validity_status=validity_status, valid_until=valid_until)
                )
        return bool(result.rowcount)

    async def delete(self, memory_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(status=MemoryStatus.DELETED.value, deleted_time=_now())
                )

    async def flush_access_stats(self) -> int:
        return 0

    @staticmethod
    def _domain(row, *, score=None):
        return MemoryRecord(
            row.id,
            row.content,
            _json(row.metadata_json, {}),
            score,
            row.source_kind,
            MemoryStatus(row.status),
        )


class PostgresRetrievalQueryRepository(RetrievalQueryPort):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def list_runs(self, **filters):
        async with self._session_factory() as session:
            query = (
                select(RetrievalRunModel)
                .order_by(RetrievalRunModel.created_at.desc())
                .offset(filters.get("offset", 0))
                .limit(filters.get("limit", 30))
            )
            if filters.get("scope"):
                query = query.where(RetrievalRunModel.scope == filters["scope"])
            if filters.get("status"):
                query = query.where(RetrievalRunModel.status == filters["status"])
            rows = (await session.execute(query)).scalars().all()
            total = (
                await session.scalar(
                    select(func.count()).select_from(RetrievalRunModel)
                )
                or 0
            )
        return [self._row(row) for row in rows], int(total)

    async def get_run(self, run_id: str):
        async with self._session_factory() as session:
            row = await session.get(RetrievalRunModel, run_id)
        return None if row is None else self._row(row)

    @staticmethod
    def _row(row):
        return {
            "run_id": row.run_id,
            "query": row.query,
            "scope": row.scope,
            "status": row.status,
            "config": _json(row.config_json, {}),
            "candidate_count": row.candidate_count,
            "selected_count": row.selected_count,
            "injected_count": row.injected_count,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }


__all__ = [
    "PostgresApprovalRepository",
    "PostgresEventRepository",
    "PostgresMemoryRepository",
    "PostgresRetrievalQueryRepository",
    "PostgresRunRepository",
]
