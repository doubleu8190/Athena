"""PostgreSQL repository for ordered Worker execution transcripts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from application.orchestration.serialization import json_value
from domain.orchestration import (
    WorkerExecutionEntryType,
    WorkerExecutionMessage,
    WorkerExecutionMessageAppend,
    WorkerExecutionMessageContent,
    WorkerExecutionMessageRepository,
    WorkerLLMRole,
)

from ..models import WorkerExecutionMessageModel


class PostgresWorkerExecutionMessageRepository(WorkerExecutionMessageRepository):
    """Append idempotent transcript records within one execution generation."""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        self._session_factory = session_factory

    async def append(
        self, message: WorkerExecutionMessageAppend
    ) -> WorkerExecutionMessage:
        content_json = json_value(message.content.model_dump(mode="json"))
        async with self._session_factory() as session:
            async with session.begin():
                lock_key = f"{message.task_id}:{message.execution_generation}"
                await session.scalar(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(lock_key, 0)
                        )
                    )
                )
                existing = await session.get(
                    WorkerExecutionMessageModel, message.message_id
                )
                if existing is not None:
                    if not _matches(existing, message, content_json):
                        raise ValueError("worker_execution_message_id_conflict")
                    return _to_domain(existing)

                sequence_no = int(
                    await session.scalar(
                        select(func.max(WorkerExecutionMessageModel.sequence_no)).where(
                            WorkerExecutionMessageModel.task_id == message.task_id,
                            WorkerExecutionMessageModel.execution_generation
                            == message.execution_generation,
                        )
                    )
                    or 0
                ) + 1
                row = WorkerExecutionMessageModel(
                    message_id=message.message_id,
                    session_id=message.session_id,
                    run_id=message.run_id,
                    plan_id=message.plan_id,
                    task_id=message.task_id,
                    execution_id=message.execution_id,
                    execution_generation=message.execution_generation,
                    invocation_id=message.invocation_id,
                    sequence_no=sequence_no,
                    entry_type=message.entry_type.value,
                    role=message.role.value if message.role is not None else None,
                    content_json=content_json,
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                    operation_key=message.operation_key,
                    result_ref=message.result_ref,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(row)
                await session.flush()
                return _to_domain(row)

    async def list_by_execution(
        self, task_id: str, execution_generation: int
    ) -> list[WorkerExecutionMessage]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(WorkerExecutionMessageModel)
                    .where(
                        WorkerExecutionMessageModel.task_id == task_id,
                        WorkerExecutionMessageModel.execution_generation
                        == execution_generation,
                    )
                    .order_by(WorkerExecutionMessageModel.sequence_no.asc())
                )
            ).scalars().all()
        return [_to_domain(row) for row in rows]


def _matches(
    row: WorkerExecutionMessageModel,
    value: WorkerExecutionMessageAppend,
    content_json: dict[str, object],
) -> bool:
    """Reject stable IDs reused for different transcript content."""
    return (
        row.session_id == value.session_id
        and row.run_id == value.run_id
        and row.plan_id == value.plan_id
        and row.task_id == value.task_id
        and row.execution_id == value.execution_id
        and row.execution_generation == value.execution_generation
        and row.invocation_id == value.invocation_id
        and row.entry_type == value.entry_type.value
        and row.role == (value.role.value if value.role is not None else None)
        and row.content_json == content_json
        and row.tool_call_id == value.tool_call_id
        and row.tool_name == value.tool_name
        and row.operation_key == value.operation_key
        and row.result_ref == value.result_ref
    )


def _to_domain(row: WorkerExecutionMessageModel) -> WorkerExecutionMessage:
    """Validate JSON persistence before returning a domain transcript row."""
    return WorkerExecutionMessage(
        message_id=row.message_id,
        session_id=row.session_id,
        run_id=row.run_id,
        plan_id=row.plan_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        execution_generation=row.execution_generation,
        invocation_id=row.invocation_id,
        sequence_no=row.sequence_no,
        entry_type=WorkerExecutionEntryType(row.entry_type),
        role=WorkerLLMRole(row.role) if row.role is not None else None,
        content=WorkerExecutionMessageContent.model_validate(row.content_json),
        created_at=row.created_at,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        operation_key=row.operation_key,
        result_ref=row.result_ref,
    )


__all__ = ["PostgresWorkerExecutionMessageRepository"]
