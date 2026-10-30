"""PostgreSQL-backed durable queue for memory extraction jobs."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseGrant, LeaseKey, LeaseLostError

from backend.src.shared.logging import get_logger
from domain.memory import (
    ClaimedMemoryJob,
    CompletedTurn,
    MemoryExecutionEvidence,
    MemoryJobPort,
    MemoryJobQueue,
    MemoryJobStatus,
    MemoryJobStatusPort,
    MemorySourceReference,
    MemoryTaskOutcomeEvidence,
    MemoryToolOutcomeEvidence,
)
from application.contracts.memory import (
    MemoryExecutionEvidenceSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)

from ..models import (
    AgentTaskModel,
    MemoryProcessingJobModel,
    MemoryRunEvidenceSnapshotModel,
    SessionModel,
    TaskExecutionModel,
    WorkerExecutionMessageModel,
)
from .lease_center import PostgresLeaseCenter
from .memory_suppression import ensure_writable_run_scope, project_scope_is_writable

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_memory_job_in_transaction(
    session: AsyncSession, *, turn_id: str, session_id: str,
    user_text: str, assistant_text: str, now: datetime,
) -> bool:
    """在调用方事务内创建唯一轮次作业；空输入或已遗忘范围返回 False。"""
    if not user_text.strip() or not await ensure_writable_run_scope(
        session, session_id=session_id, turn_id=turn_id, now=now,
    ):
        return False
    inserted = await session.scalar(pg_insert(MemoryProcessingJobModel).values(
        job_id=f"memjob_{uuid.uuid4().hex}", turn_id=turn_id, session_id=session_id,
        user_text=user_text, assistant_text=assistant_text, status="queued", attempt=0,
        available_at=now, created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["turn_id"]).returning(MemoryProcessingJobModel.job_id))
    return inserted is not None


class PostgresMemoryJobRepository(MemoryJobPort, MemoryJobQueue, MemoryJobStatusPort):
    """Persist memory jobs so extraction survives process restarts."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        max_attempts: int = 3,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def list_recent(
        self,
        *,
        limit: int,
        offset: int,
        session_id: str | None = None,
    ) -> tuple[list[MemoryJobStatus], int]:
        """List bounded, redacted job status rows for the management UI."""
        bounded_limit = min(100, max(1, limit))
        bounded_offset = max(0, offset)
        conditions = []
        if session_id:
            conditions.append(MemoryProcessingJobModel.session_id == session_id)
        async with self._session_factory() as session:
            total = (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryProcessingJobModel)
                    .where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(
                        MemoryProcessingJobModel,
                        SessionModel.project_id,
                    )
                    .outerjoin(
                        SessionModel,
                        SessionModel.id == MemoryProcessingJobModel.session_id,
                    )
                    .where(*conditions)
                    .order_by(
                        MemoryProcessingJobModel.created_at.desc(),
                        MemoryProcessingJobModel.turn_id,
                    )
                    .offset(bounded_offset)
                    .limit(bounded_limit)
                )
            ).all()
        return (
            [
                MemoryJobStatus(
                    turn_id=row.turn_id,
                    session_id=row.session_id,
                    project_id=project_id,
                    status=row.status,
                    attempt=row.attempt,
                    created_at=row.created_at.isoformat(),
                    updated_at=row.updated_at.isoformat(),
                    available_at=row.available_at.isoformat(),
                )
                for row, project_id in rows
            ],
            int(total),
        )

    async def enqueue(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str = "",
    ) -> bool:
        if not user_text.strip():
            return False
        async with self._session_factory() as session:
            project_id = await session.scalar(
                select(SessionModel.project_id).where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if project_id is not None and not await project_scope_is_writable(
                session, project_id
            ):
                return False
            active_session_id = await session.scalar(
                select(SessionModel.id)
                .where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if active_session_id is None:
                return False
            now = _now()
            writable = await ensure_writable_run_scope(
                session,
                session_id=session_id,
                turn_id=turn_id,
                now=now,
            )
            if not writable:
                await session.commit()
                return False
            existing = await session.scalar(
                select(MemoryProcessingJobModel).where(
                    MemoryProcessingJobModel.turn_id == turn_id
                )
            )
            if existing is not None:
                if existing.status == "failed":
                    existing.status = "queued"
                    existing.attempt = 0
                    existing.available_at = now
                    existing.updated_at = now
                    await self._leases.schedule_in_transaction(
                        session, LeaseKey("memory_processing", turn_id)
                    )
                    await session.commit()
                    return True
                await session.commit()
                return False
            await enqueue_memory_job_in_transaction(session, turn_id=turn_id,
                session_id=session_id, user_text=user_text, assistant_text=assistant_text, now=now)
            await self._leases.ensure_in_transaction(
                session, LeaseKey("memory_processing", turn_id)
            )
            await session.commit()
            return True

    async def claim_next_job(
        self, *, lease_seconds: float = 60.0
    ) -> ClaimedMemoryJob | None:
        """领取一条记忆作业并加载其首次冻结的执行证据。

        参数：无。
        返回：可处理的作业及其尝试次数；队列为空时返回 None。
        异常：数据库读取、快照校验或写入失败时向上传播。
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._session_factory() as session:
            async with session.begin():
                now = _now()
                after_resource_id: str | None = None
                while True:
                    claimable = await self._leases.claimable_keys_in_transaction(
                        session,
                        "memory_processing",
                        limit=128,
                        after_resource_id=after_resource_id,
                    )
                    if not claimable:
                        return None
                    candidate_ids = [key.resource_id for key in claimable]
                    candidates = (await session.scalars(
                        select(MemoryProcessingJobModel)
                        .where(
                            MemoryProcessingJobModel.turn_id.in_(candidate_ids),
                            or_(
                                and_(
                                    MemoryProcessingJobModel.status == "queued",
                                    MemoryProcessingJobModel.available_at <= now,
                                ),
                                MemoryProcessingJobModel.status == "processing",
                            ),
                        )
                        .order_by(MemoryProcessingJobModel.created_at)
                        .with_for_update(skip_locked=True)
                        .limit(len(candidate_ids))
                    )).all()
                    for row in candidates:
                        project_id = await session.scalar(
                            select(SessionModel.project_id).where(
                                SessionModel.id == row.session_id
                            )
                        )
                        if project_id is None:
                            logger.warning(
                                "没找到session隶属的项目，可能已被删除，跳过记忆作业。",
                                extra={
                                    "turn_id": row.turn_id,
                                    "session_id": row.session_id,
                                },
                            )
                            await self._leases.complete_unclaimed_in_transaction(
                                session, LeaseKey("memory_processing", row.turn_id)
                            )
                            await session.delete(row)
                            continue
                        grant = await self._leases.acquire_in_transaction(
                            session,
                            LeaseKey("memory_processing", row.turn_id),
                            owner_id=uuid.uuid4().hex,
                            lease_seconds=lease_seconds,
                        )
                        if grant is None:
                            logger.info(
                                "memory_job_lease_conflict",
                                extra={
                                    "turn_id": row.turn_id,
                                    "session_id": row.session_id,
                                },
                            )
                            continue
                        execution_evidence = await _load_or_freeze_execution_evidence(
                            session,
                            turn_id=row.turn_id,
                            observed_at=row.created_at,
                        )
                        if row.status == "queued":
                            row.attempt += 1
                        row.status = "processing"
                        row.updated_at = now
                        return ClaimedMemoryJob(
                            turn=CompletedTurn(
                                turn_id=row.turn_id,
                                session_id=row.session_id,
                                user_text=row.user_text,
                                assistant_text=row.assistant_text,
                                completed_at=row.created_at,
                                execution_evidence=execution_evidence,
                                project_id=project_id,
                            ),
                            attempt=row.attempt,
                            lease=grant,
                        )
                    after_resource_id = claimable[-1].resource_id

    async def mark_succeeded(self, turn_id: str, *, lease: LeaseGrant) -> None:
        if lease.key != LeaseKey("memory_processing", turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryProcessingJobModel)
                    .where(MemoryProcessingJobModel.turn_id == turn_id)
                    .with_for_update()
                )
                if row is None or row.status != "processing":
                    return
                await self._leases.assert_current_in_transaction(session, lease)
                row.status = "succeeded"
                row.error_json = None
                row.updated_at = _now()
                if not await self._leases.finish_in_transaction(
                    session, lease, completed=True
                ):
                    raise LeaseLostError(lease.key)

    async def mark_failed(
        self, turn_id: str, error: str, *, retry: bool, lease: LeaseGrant
    ) -> None:
        """记录失败并按剩余尝试次数安排退避重试。

        参数：
            turn_id: 失败记忆作业所属轮次。
            error: 本次失败原因，写入作业错误摘要。
            retry: 调用方判断本次失败是否可以重试。
        返回：无；找不到对应作业时直接结束。
        异常：数据库更新失败时向上传播。
        """
        if lease.key != LeaseKey("memory_processing", turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryProcessingJobModel)
                    .where(MemoryProcessingJobModel.turn_id == turn_id)
                    .with_for_update()
                )
                if row is None or row.status != "processing":
                    return
                await self._leases.assert_current_in_transaction(session, lease)
                row.error_json = {"message": error}
                row.updated_at = _now()
                if retry and row.attempt < self._max_attempts:
                    delay = min(300, 5 * (2 ** max(0, row.attempt - 1)))
                    row.status = "queued"
                    row.available_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    )
                else:
                    row.status = "failed"
                if not await self._leases.finish_in_transaction(
                    session, lease, completed=not (retry and row.attempt < self._max_attempts)
                ):
                    raise LeaseLostError(lease.key)


async def _load_or_freeze_execution_evidence(
    session: AsyncSession,
    *,
    turn_id: str,
    observed_at: str,
) -> MemoryExecutionEvidence | None:
    """首次领取时冻结 Run 的有限任务状态和 Worker 回执引用。

    参数：
        session: 当前领取事务使用的 PostgreSQL 会话。
        turn_id: 与 Run ID 相同的已完成轮次标识。
        observed_at: 入队时间，作为 Run 级证据的观察时间。
    返回：已有或新建的不可变执行证据；没有计划任务时返回 None。
    异常：持久快照与严格契约不匹配或数据库读写失败时向上传播。
    """
    frozen_row = await session.get(MemoryRunEvidenceSnapshotModel, turn_id)
    if frozen_row is not None:
        # 重试时优先获取上次执行任务时收集的证据
        evidence = memory_schema_to_domain(
            MemoryExecutionEvidenceSchema.model_validate(frozen_row.snapshot_json)
        )
        if evidence.source_fingerprint != frozen_row.source_fingerprint:
            raise RuntimeError("memory_run_evidence_fingerprint_mismatch")
        logger.info("发现已有执行证据，使用缓存数据。", extra={"turn_id": turn_id})
        return evidence

    # 执行到这一步，说明是第一次执行此轮任务，因此需要开始构建证据
    total_task_count = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentTaskModel)
            .where(AgentTaskModel.run_id == turn_id)
        )
        or 0
    )
    if total_task_count == 0:
        logger.info("memory_run_evidence_no_tasks", extra={"turn_id": turn_id})
        return None

    task_rows = (
        (
            await session.execute(
                select(AgentTaskModel)
                .where(AgentTaskModel.run_id == turn_id)
                .order_by(AgentTaskModel.created_at, AgentTaskModel.task_id)
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    task_ids = [task.task_id for task in task_rows]
    task_generations = {task.task_id: task.execution_generation for task in task_rows}
    execution_rows = (
        (
            await session.execute(
                select(TaskExecutionModel).where(
                    TaskExecutionModel.task_id.in_(task_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    executions_by_task_generation = {
        (execution.task_id, execution.attempt): execution
        for execution in execution_rows
    }
    message_rows = (
        (
            await session.execute(
                select(WorkerExecutionMessageModel)
                .where(
                    WorkerExecutionMessageModel.run_id == turn_id,
                    WorkerExecutionMessageModel.task_id.in_(task_ids),
                    WorkerExecutionMessageModel.entry_type == "tool_result",
                )
                .order_by(
                    WorkerExecutionMessageModel.created_at,
                    WorkerExecutionMessageModel.sequence_no,
                )
                .limit(1001)
            )
        )
        .scalars()
        .all()
    )
    message_overflow = len(message_rows) > 1000
    messages_by_task: dict[str, list[WorkerExecutionMessageModel]] = {}
    for message in message_rows[:1000]:
        if task_generations.get(message.task_id) != message.execution_generation:
            continue
        messages_by_task.setdefault(message.task_id, []).append(message)

    task_evidence: list[MemoryTaskOutcomeEvidence] = []
    source_refs: list[MemorySourceReference] = []
    truncated = total_task_count > len(task_rows) or message_overflow
    task_statuses: list[str] = []
    for task in task_rows:
        task_messages = messages_by_task.get(task.task_id, [])
        if len(task_messages) > 4:
            truncated = True
        tool_outcomes: list[MemoryToolOutcomeEvidence] = []
        for message in task_messages[-4:]:
            content = message.content_json
            tool_result = (
                content.get("tool_result") if isinstance(content, dict) else None
            )
            tool_result = tool_result if isinstance(tool_result, dict) else {}
            status = tool_result.get("status")
            if not isinstance(status, str) or not status:
                status = "unknown"
            tool_name = message.tool_name or "unknown"
            result_ref = message.result_ref or tool_result.get("result_ref")
            if not isinstance(result_ref, str):
                result_ref = None
            message_observed_at = message.created_at.isoformat()
            tool_outcomes.append(
                MemoryToolOutcomeEvidence(
                    message_id=message.message_id,
                    tool_name=tool_name,
                    status=status[:40],
                    result_ref=result_ref,
                    observed_at=message_observed_at,
                )
            )
            source_refs.append(
                MemorySourceReference(
                    source_type="worker_message",
                    source_id=message.message_id,
                    excerpt=f"{tool_name[:120]} status={status[:40]}",
                    observed_at=message_observed_at,
                )
            )

        task_status = task.status or "unknown"
        task_statuses.append(f"{task.task_id}={task_status}")
        execution = executions_by_task_generation.get(
            (task.task_id, task.execution_generation)
        )
        result_ref = execution.result_ref if execution is not None else None
        error_code = _error_code(task.error_json)
        task_evidence.append(
            MemoryTaskOutcomeEvidence(
                task_id=task.task_id,
                title=(task.title or task.task_id)[:300],
                status=task_status[:40],
                execution_generation=task.execution_generation,
                error_code=error_code[:100] if error_code else None,
                result_ref=result_ref,
                tool_outcomes=tuple(tool_outcomes),
            )
        )
        if result_ref:
            source_refs.append(
                MemorySourceReference(
                    source_type="artifact",
                    source_id=result_ref,
                    excerpt=f"Task result artifact for {task.task_id}",
                    observed_at=task.updated_at or observed_at,
                )
            )

    run_excerpt = "Run task outcomes: " + "; ".join(task_statuses)
    source_refs.insert(
        0,
        MemorySourceReference(
            source_type="run",
            source_id=turn_id,
            excerpt=run_excerpt[:2000],
            observed_at=observed_at,
        ),
    )
    evidence_payload = {
        "run_id": turn_id,
        "total_task_count": total_task_count,
        "truncated": truncated,
        "tasks": [
            memory_domain_to_schema(task).model_dump(mode="json")
            for task in task_evidence
        ],
        "source_refs": [
            memory_domain_to_schema(source_ref).model_dump(mode="json")
            for source_ref in source_refs[:80]
        ],
    }
    evidence_canonical = json.dumps(
        evidence_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_fingerprint = hashlib.sha256(evidence_canonical.encode("utf-8")).hexdigest()
    frozen_payload = {
        **evidence_payload,
        "source_fingerprint": source_fingerprint,
    }
    canonical_json = json.dumps(
        frozen_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_schema = MemoryExecutionEvidenceSchema.model_validate_json(canonical_json)
    evidence = memory_schema_to_domain(evidence_schema)
    await session.execute(
        pg_insert(MemoryRunEvidenceSnapshotModel)
        .values(
            turn_id=turn_id,
            source_fingerprint=source_fingerprint,
            snapshot_json=frozen_payload,
            created_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=["turn_id"])
    )
    frozen_row = await session.get(MemoryRunEvidenceSnapshotModel, turn_id)
    if frozen_row is None:
        raise RuntimeError("memory_run_evidence_snapshot_missing")
    frozen_evidence = memory_schema_to_domain(
        MemoryExecutionEvidenceSchema.model_validate(frozen_row.snapshot_json)
    )
    if frozen_evidence.source_fingerprint != frozen_row.source_fingerprint:
        raise RuntimeError("memory_run_evidence_fingerprint_mismatch")
    return frozen_evidence


def _error_code(error_json: dict[str, Any] | None) -> str | None:
    """只从任务错误摘要中读取稳定错误码，不把堆栈带入记忆。

    参数：``error_json`` 是任务终态保存的 JSONB 错误摘要。
    返回：最多 100 字符的错误码；没有稳定错误码时返回 None。
    异常：非法 JSON 被视为没有错误码，不向上传播。
    """
    code = error_json.get("code") if error_json is not None else None
    return code[:100] if isinstance(code, str) and code else None


__all__ = ["PostgresMemoryJobRepository"]
