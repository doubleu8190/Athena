"""Shared PostgreSQL persistence implementations for runtime capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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
from domain.runs.ports import CommandQueuePort, CommandStorePort, RunLifecyclePort

from ..models import (
    AgentCommandModel,
    AgentRunModel,
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


class PostgresRunRepository(
    CommandQueuePort,
    CommandStorePort,
    RunLifecyclePort,
    RunQueryPort,
):
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

    async def complete_active_command_for_run(
        self,
        run_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> bool:
        """Converge the original command after an internal approval resume."""
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentCommandModel)
                        .where(
                            AgentCommandModel.run_id == run_id,
                            AgentCommandModel.status == CommandStatus.RUNNING.value,
                        )
                        .order_by(AgentCommandModel.started_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return False
                row.status = str(status)
                row.result_json = json.dumps(result, default=str) if result is not None else None
                row.error_json = json.dumps(error, default=str) if error is not None else None
                row.completed_at = _now()
                return True

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

__all__ = [
    "PostgresRunRepository",
]
