"""PostgreSQL Trace span 与不可变 Progress 仓储。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from application.observability.contracts import (
    ProgressAppend,
    ProgressEntry,
    ProgressSource,
    TraceSpan,
    TraceSpanAttributes,
    WorkerProgressContext,
    sanitize_trace_error,
)
from application.observability.service import (
    ProgressConflictError,
    ProgressExecutionTerminalError,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    RunProgressModel,
    TraceSpanModel,
)

from .progress_locking import lock_progress_session
from .trace_locking import lock_trace_span_scope

_TRACE_ATTRIBUTES = TypeAdapter(TraceSpanAttributes)


class PostgresObservabilityRepository:
    """在独立事务中保存 Trace 和 Progress。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def append_progress(self, value: ProgressAppend) -> ProgressEntry:
        """插入完整 Progress；并发重放返回已有稳定记录。"""
        scope_key = (
            f"execution:{value.execution_id}"
            if value.execution_id
            else f"run:{value.run_id}"
        )
        progress_id = uuid4().hex
        created_at = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await lock_progress_session(session, value.session_id)
            row = RunProgressModel(
                progress_id=progress_id,
                session_id=value.session_id,
                run_id=value.run_id,
                execution_id=value.execution_id,
                task_id=value.task_id,
                worker_id=value.worker_id,
                source=value.source.value,
                scope_key=scope_key,
                sequence=value.sequence,
                content=value.content,
                created_at=created_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self.find_by_scope(session, scope_key, value.sequence)
                if existing is None or existing.content != value.content:
                    raise ProgressConflictError("progress_sequence_conflict")
                return existing
            await session.refresh(row)
            return _progress_from_model(row)

    async def find_by_scope(
        self, session: AsyncSession, scope_key: str, sequence: int
    ) -> ProgressEntry | None:
        row = await session.scalar(
            select(RunProgressModel).where(
                RunProgressModel.scope_key == scope_key,
                RunProgressModel.sequence == sequence,
            )
        )
        return None if row is None else _progress_from_model(row)

    async def find_progress(
        self, *, execution_id: str, sequence: int
    ) -> ProgressEntry | None:
        if not execution_id:
            return None
        async with self._session_factory() as session:
            return await self.find_by_scope(
                session, f"execution:{execution_id}", sequence
            )

    async def list_progress_for_run(self, run_id: str) -> list[ProgressEntry]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(RunProgressModel)
                    .where(RunProgressModel.run_id == run_id)
                    .order_by(
                        RunProgressModel.ingest_sequence,
                        RunProgressModel.sequence,
                    )
                )
            ).scalars()
            return [_progress_from_model(row) for row in rows]

    async def list_progress_after(
        self, session_id: str, after_ingest_sequence: int
    ) -> list[ProgressEntry]:
        """按会话和全局写入水位读取后续 Progress。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        RunProgressModel,
                        func.coalesce(
                            func.nullif(
                                OrchestratorExecutionModel.worker_name, ""
                            ),
                            OrchestratorExecutionModel.instance_id,
                        ),
                        OrchestratorTaskModel.objective,
                    )
                    .outerjoin(
                        OrchestratorExecutionModel,
                        OrchestratorExecutionModel.execution_id
                        == RunProgressModel.execution_id,
                    )
                    .outerjoin(
                        OrchestratorTaskModel,
                        OrchestratorTaskModel.task_id
                        == RunProgressModel.task_id,
                    )
                    .where(
                        RunProgressModel.session_id == session_id,
                        RunProgressModel.ingest_sequence > after_ingest_sequence,
                    )
                    .order_by(RunProgressModel.ingest_sequence)
                )
            ).all()
            return [
                _progress_from_model(
                    row[0],
                    worker_name=row[1],
                    task_title=row[2],
                )
                for row in rows
            ]

    async def max_progress_ingest_sequence(self, session_id: str) -> int:
        """返回会话当前最大的 Progress 写入水位。"""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.max(RunProgressModel.ingest_sequence)).where(
                    RunProgressModel.session_id == session_id
                )
            )
            return int(value or 0)

    async def progress_ingest_watermark(self) -> int:
        """返回所有会话共享的 Progress Identity 水位。"""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.max(RunProgressModel.ingest_sequence))
            )
            return int(value or 0)

    async def max_worker_progress_sequence(self, execution_id: str) -> int:
        """返回外部 Worker 已成功接收的最大业务序号。"""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.max(RunProgressModel.sequence)).where(
                    RunProgressModel.execution_id == execution_id,
                    RunProgressModel.source == ProgressSource.WORKER.value,
                )
            )
            return int(value or 0)

    async def append_worker_progress(
        self,
        context: WorkerProgressContext,
        *,
        sequence: int,
        content: str,
    ) -> ProgressEntry:
        """锁定执行身份后接收 Progress，保证与终态结果提交互斥。"""
        scope_key = f"execution:{context.execution_id}"
        async with self._session_factory() as session, session.begin():
            # Progress 锁必须先于 Execution 行锁：dispatcher 也按 Progress →
            # Execution 顺序写入开始记录。反向获取会让外键等待与 advisory 等待成环。
            await lock_progress_session(session, context.session_id)
            execution = await session.get(
                OrchestratorExecutionModel, context.execution_id, with_for_update=True
            )
            if execution is None or execution.instance_id != context.worker_id:
                raise ValueError("worker_progress_execution_identity_unknown")
            row = await session.scalar(
                select(RunProgressModel)
                .where(
                    RunProgressModel.scope_key == scope_key,
                    RunProgressModel.sequence == sequence,
                )
                .with_for_update()
            )
            if row is not None:
                if row.content != content:
                    raise ProgressConflictError("progress_sequence_conflict")
                return _progress_from_model(
                    row,
                    worker_name=context.worker_name,
                    task_title=context.task_title,
                )
            # 精确重放先于终态检查，网络重试可以在 execution 结束后仍取回原回执；
            # 但任何新序号都不能写入已经收束的执行。
            if execution.status not in {"running", "waiting_approval"}:
                raise ProgressExecutionTerminalError(
                    "worker_execution_already_terminal"
                )
            existing_sequences = list(
                (
                    await session.scalars(
                        select(RunProgressModel.sequence)
                        .where(
                            RunProgressModel.scope_key == scope_key,
                            RunProgressModel.source == ProgressSource.WORKER.value,
                        )
                        .order_by(RunProgressModel.sequence)
                    )
                ).all()
            )
            # execution 行锁把并发上报串行化；只接受从 1 开始的连续下一项，
            # 从中间插入或跳号都必须由调用方补发缺失项。
            if (
                existing_sequences
                != list(range(1, len(existing_sequences) + 1))
                or sequence != len(existing_sequences) + 1
            ):
                raise ProgressConflictError("worker_progress_sequence_incomplete")
            created_at = await session.scalar(select(func.clock_timestamp()))
            if created_at is None:
                raise RuntimeError("database_clock_unavailable")
            progress = RunProgressModel(
                progress_id=uuid4().hex,
                session_id=context.session_id,
                run_id=context.run_id,
                execution_id=context.execution_id,
                task_id=context.task_id,
                worker_id=context.worker_id,
                source=ProgressSource.WORKER.value,
                scope_key=scope_key,
                sequence=sequence,
                content=content,
                created_at=created_at,
            )
            session.add(progress)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ProgressConflictError("progress_sequence_conflict") from exc
            await session.refresh(progress)
            return _progress_from_model(
                progress,
                worker_name=context.worker_name,
                task_title=context.task_title,
            )

    async def worker_progress_context(
        self, *, execution_id: str, worker_id: str
    ) -> WorkerProgressContext | None:
        """按认证实例解析 Worker 不得自行声明的执行归属。"""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        OrchestratorExecutionModel.execution_id,
                        OrchestratorExecutionModel.task_id,
                        OrchestratorExecutionModel.instance_id,
                        OrchestratorExecutionModel.worker_name,
                        OrchestratorPlanModel.run_id,
                        AgentRunModel.session_id,
                        OrchestratorTaskModel.objective,
                    )
                    .join(
                        OrchestratorTaskModel,
                        OrchestratorTaskModel.task_id
                        == OrchestratorExecutionModel.task_id,
                    )
                    .join(
                        OrchestratorPlanModel,
                        OrchestratorPlanModel.plan_id == OrchestratorTaskModel.plan_id,
                    )
                    .join(
                        AgentRunModel,
                        AgentRunModel.run_id == OrchestratorPlanModel.run_id,
                    )
                    .where(
                        OrchestratorExecutionModel.execution_id == execution_id,
                        OrchestratorExecutionModel.instance_id == worker_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return WorkerProgressContext(
            execution_id=row.execution_id,
            task_id=row.task_id,
            run_id=row.run_id,
            session_id=row.session_id,
            worker_id=row.instance_id,
            worker_name=row.worker_name or row.instance_id,
            task_title=row.objective,
        )

    async def start_span(
        self,
        *,
        span_id: str,
        session_id: str,
        run_id: str,
        parent_span_id: str | None,
        name: str,
        started_at: datetime,
        attributes: TraceSpanAttributes,
    ) -> TraceSpan:
        """以 Run 级 advisory lock 分配稳定 span 顺序。"""
        async with self._session_factory() as session:
            async with session.begin():
                await lock_trace_span_scope(session, run_id)
                sequence = int(
                    await session.scalar(
                        select(func.coalesce(func.max(TraceSpanModel.sequence), 0))
                        .where(TraceSpanModel.run_id == run_id)
                    )
                    or 0
                ) + 1
                row = TraceSpanModel(
                    span_id=span_id,
                    session_id=session_id,
                    run_id=run_id,
                    parent_span_id=parent_span_id,
                    span_type=attributes.span_type,
                    name=name,
                    status="running",
                    started_at=started_at,
                    attributes_json=attributes.model_dump(mode="json"),
                    sequence=sequence,
                )
                session.add(row)
            await session.refresh(row)
            return _span_from_model(row)

    async def finish_span(
        self,
        *,
        span_id: str,
        status: str,
        completed_at: datetime,
        duration_ms: int | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        """结束并非 running 的 span；不存在时保持幂等。"""
        async with self._session_factory() as session:
            row = await session.get(TraceSpanModel, span_id)
            if row is None or row.status != "running":
                return
            del duration_ms
            actual_duration = max(
                0, int((completed_at - row.started_at).total_seconds() * 1000)
            )
            row.status = status
            row.completed_at = completed_at
            row.duration_ms = actual_duration
            row.error_json = sanitize_trace_error(error)
            await session.commit()

    async def list_spans_for_run(self, run_id: str) -> list[TraceSpan]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(TraceSpanModel)
                    .where(TraceSpanModel.run_id == run_id)
                    .order_by(TraceSpanModel.sequence)
                )
            ).scalars()
            return [_span_from_model(row) for row in rows]


def _progress_from_model(
    row: RunProgressModel,
    *,
    worker_name: str | None = None,
    task_title: str | None = None,
) -> ProgressEntry:
    return ProgressEntry(
        progress_id=row.progress_id,
        session_id=row.session_id,
        run_id=row.run_id,
        execution_id=row.execution_id,
        task_id=row.task_id,
        worker_id=row.worker_id,
        source=ProgressSource(row.source),
        sequence=row.sequence,
        content=row.content,
        created_at=row.created_at,
        ingest_sequence=row.ingest_sequence,
        worker_name=worker_name,
        task_title=task_title,
    )


def _span_from_model(row: TraceSpanModel) -> TraceSpan:
    return TraceSpan(
        span_id=row.span_id,
        session_id=row.session_id,
        run_id=row.run_id,
        parent_span_id=row.parent_span_id,
        span_type=row.span_type,
        name=row.name,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        attributes=_TRACE_ATTRIBUTES.validate_python(row.attributes_json),
        error=sanitize_trace_error(row.error_json),
        sequence=row.sequence,
    )


__all__ = ["PostgresObservabilityRepository"]
