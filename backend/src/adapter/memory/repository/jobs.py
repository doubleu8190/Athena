"""记忆生命周期仓储：后台任务调度与运维子模块。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.memory.model import (
    MemoryJobModel,
    MemoryModel,
    MemoryScopeStateModel,
)
from adapter.memory.repository._shared import (
    _job_payload,
    _job_to_domain,
    _json_value,
    _MemoryRepositoryHelpersMixin,
    enqueue_cleanup_job,
    ensure_scan_job_locked,
    lock_scope_state,
    lock_user_memory_setting,
)
from adapter.run.model import RunModel
from common.date import as_utc, utc_now
from domain.memory.lifecycle import (
    CleanupResult,
    CompleteJobCommand,
    ExtractionStatus,
    ExtractPendingPayload,
    JobKind,
    JobStatus,
    MaintainResult,
    MaintainScopePayload,
    MemoryJob,
    ScanJobResult,
    ScanResult,
    ScanRunsPayload,
)

_BACKGROUND_KINDS = (
    JobKind.EXTRACT_PENDING,
    JobKind.MAINTAIN_SCOPE,
    JobKind.CLEANUP_SCOPE,
    JobKind.SCAN_RUNS,
)
_MAX_EXTRACT_ATTEMPTS = 5


class MemoryJobsRepositoryMixin(_MemoryRepositoryHelpersMixin):
    """负责后台任务的唤醒、领取、完成、扫描、每日归档和 scope 清理。"""

    async def wake_user(self, owner_id: str) -> None:
        """刷新用户级提取任务；单 Worker 顺序消费。"""
        if not owner_id.strip():
            raise ValueError("memory_owner_required")
        now = utc_now()
        dedupe_key = f"extract_pending:{owner_id}"
        async with self._factory() as session, session.begin():
            await session.execute(
                insert(MemoryJobModel)
                .values(
                    id=f"mjob_{hashlib.sha256(dedupe_key.encode()).hexdigest()[:24]}",
                    dedupe_key=dedupe_key,
                    kind=JobKind.EXTRACT_PENDING.value,
                    status=JobStatus.QUEUED.value,
                    payload=_json_value(ExtractPendingPayload(owner_id=owner_id)),
                    result=None,
                    namespace_id=owner_id,
                    scope_key=None,
                    attempt_count=0,
                    schema_retry_count=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )
            row = await session.scalar(
                select(MemoryJobModel)
                .where(MemoryJobModel.dedupe_key == dedupe_key)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("memory_wakeup_job_missing")
            row.available_at = now
            row.updated_at = now
            if row.status != JobStatus.RUNNING.value:
                row.status = JobStatus.QUEUED.value
                row.result = None

    async def ensure_scan_job(self, enabled_boundary_at: datetime) -> MemoryJob:
        """创建固定扫描记录，后续唤醒只刷新可用时间。"""
        async with self._factory() as session, session.begin():
            row = await ensure_scan_job_locked(session, enabled_boundary_at)
            return _job_to_domain(row)

    async def recover_running_jobs(self) -> int:
        """启动时把遗留 running 后台任务恢复为 queued。"""
        now = utc_now()
        async with self._factory() as session, session.begin():
            result = await session.execute(
                update(MemoryJobModel)
                .where(
                    MemoryJobModel.status == JobStatus.RUNNING.value,
                    MemoryJobModel.kind.in_(tuple(kind.value for kind in _BACKGROUND_KINDS)),
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    available_at=now,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    async def claim_job(
        self,
        *,
        kinds: tuple[JobKind, ...],
        limit: int = 1,
    ) -> MemoryJob | None:
        """领取一个 queued 后台任务并转 running；单 Worker 顺序处理。"""
        if not kinds or any(kind not in _BACKGROUND_KINDS for kind in kinds) or limit != 1:
            raise ValueError("memory_claim_invalid")
        now = utc_now()
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(MemoryJobModel)
                .where(
                    MemoryJobModel.kind.in_(tuple(kind.value for kind in kinds)),
                    MemoryJobModel.status == JobStatus.QUEUED.value,
                    MemoryJobModel.available_at <= now,
                )
                .order_by(MemoryJobModel.available_at, MemoryJobModel.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.status = JobStatus.RUNNING.value
            row.attempt_count += 1
            row.updated_at = now
            await session.flush()
            return _job_to_domain(row)

    async def complete_job(self, command: CompleteJobCommand) -> bool:
        """完成、退避重排或失败一个后台任务。"""
        now = utc_now()
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(MemoryJobModel)
                .where(
                    MemoryJobModel.id == command.job_id,
                    MemoryJobModel.status == JobStatus.RUNNING.value,
                )
                .with_for_update()
            )
            if row is None:
                return False
            payload = _job_payload(row.payload)
            should_retry = command.retryable
            if command.schema_invalid:
                row.schema_retry_count += 1
                should_retry = row.schema_retry_count <= 1
            if should_retry and row.attempt_count < _MAX_EXTRACT_ATTEMPTS:
                row.status = JobStatus.QUEUED.value
                row.available_at = now + timedelta(
                    seconds=min(300, 2 ** max(0, row.attempt_count - 1))
                )
            elif should_retry or command.safe_error_code is not None:
                row.status = JobStatus.FAILED.value
            else:
                row.status = JobStatus.DONE.value
            row.result = None if command.result is None else _json_value(command.result)
            if (
                isinstance(payload, ScanRunsPayload)
                and command.result is not None
                and isinstance(command.result, ScanJobResult)
            ):
                result = command.result.result
                row.payload = _json_value(
                    payload.model_copy(
                        update={
                            "cursor_finished_at": (
                                None if result.round_complete else result.next_cursor_finished_at
                            ),
                            "cursor_run_id": (
                                None if result.round_complete else result.next_cursor_run_id
                            ),
                            "scan_round": payload.scan_round + (1 if result.round_complete else 0),
                        }
                    )
                )
                if row.status == JobStatus.DONE.value:
                    # 每轮扫描成功即结束一段连续失败计数，周期任务才能长期存活。
                    row.attempt_count = 0
                    row.schema_retry_count = 0
                    row.status = JobStatus.QUEUED.value
                    row.available_at = now + timedelta(seconds=60)
            if (
                isinstance(payload, ExtractPendingPayload)
                and row.status == JobStatus.FAILED.value
                and command.failed_run_ids
            ):
                # 仅将当前失败批次涉及的 Run 标为 FAILED，不波及该用户新到达的其他 PENDING Run。
                await self._fail_batch_runs_locked(
                    session,
                    payload.owner_id,
                    command.failed_run_ids,
                    command.safe_error_code or "memory_worker_job_failed",
                    now,
                )
            if row.status == JobStatus.DONE.value and not isinstance(payload, ScanRunsPayload):
                row.attempt_count = 0
                row.schema_retry_count = 0
            row.updated_at = now
        return True

    async def _fail_batch_runs_locked(
        self,
        session: AsyncSession,
        owner_id: str,
        run_ids: tuple[str, ...],
        safe_error_code: str,
        now: datetime,
    ) -> None:
        """提取任务最终失败时在同一事务仅把当前批次仍 pending 的 Run 标为 failed。"""
        if not run_ids:
            return
        rows = (
            await session.scalars(
                select(RunModel)
                .where(
                    RunModel.owner_id == owner_id,
                    RunModel.id.in_(run_ids),
                    RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                )
                .order_by(RunModel.id)
                .with_for_update()
            )
        ).all()
        for row in rows:
            row.memory_extraction_status = ExtractionStatus.FAILED.value
            row.memory_extracted_at = now
            row.memory_extraction_safe_error_code = safe_error_code
            row.updated_at = now

    async def scan_missing_runs(
        self,
        payload: ScanRunsPayload,
        *,
        page_limit: int = 100,
    ) -> ScanResult:
        """有界扫描 pending Run，并只唤醒去重后的用户。"""
        if not 1 <= page_limit <= 500:
            raise ValueError("memory_scan_page_limit_invalid")
        after_time = payload.cursor_finished_at
        after_id = payload.cursor_run_id
        conditions: list[Any] = [
            RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
            RunModel.finished_at.is_not(None),
            RunModel.finished_at >= payload.enabled_boundary_at,
        ]
        if after_time is not None:
            conditions.append(
                or_(
                    RunModel.finished_at > after_time,
                    (RunModel.finished_at == after_time) & (RunModel.id > (after_id or "")),
                )
            )
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(RunModel)
                    .where(*conditions)
                    .order_by(RunModel.finished_at, RunModel.id)
                    .limit(page_limit + 1)
                )
            ).all()
        page = rows[:page_limit]
        owners = tuple(dict.fromkeys(row.owner_id for row in page))
        for owner_id in owners:
            await self.wake_user(owner_id)
        has_more = len(rows) > page_limit
        last = page[-1] if page else None
        return ScanResult(
            scanned=len(page),
            awakened=len(owners),
            has_more=has_more,
            next_cursor_finished_at=(
                None if last is None or last.finished_at is None else as_utc(last.finished_at)
            ),
            next_cursor_run_id=None if last is None else last.id,
            round_complete=not has_more,
        )

    async def enqueue_maintenance(self, day: str) -> int:
        """为所有启用 scope 创建按日幂等维护任务。"""
        datetime.strptime(day, "%Y-%m-%d")
        now = utc_now()
        created = 0
        async with self._factory() as session, session.begin():
            states = (
                await session.scalars(
                    select(MemoryScopeStateModel)
                    .where(MemoryScopeStateModel.enabled.is_(True))
                    .order_by(
                        MemoryScopeStateModel.namespace_id,
                        MemoryScopeStateModel.scope_key,
                    )
                )
            ).all()
            for state in states:
                dedupe_key = f"maintain_scope:{state.namespace_id}:{state.scope_key}:{day}"
                result = await session.execute(
                    insert(MemoryJobModel)
                    .values(
                        id=f"mjob_{hashlib.sha256(dedupe_key.encode()).hexdigest()[:24]}",
                        dedupe_key=dedupe_key,
                        kind=JobKind.MAINTAIN_SCOPE.value,
                        status=JobStatus.QUEUED.value,
                        payload=_json_value(
                            MaintainScopePayload(
                                namespace_id=state.namespace_id,
                                scope_key=state.scope_key,
                                day=day,
                            )
                        ),
                        result=None,
                        namespace_id=state.namespace_id,
                        scope_key=state.scope_key,
                        attempt_count=0,
                        schema_retry_count=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["dedupe_key"])
                    .returning(MemoryJobModel.id)
                )
                if result.scalar_one_or_none() is not None:
                    created += 1
        return created

    async def maintain_scope(
        self,
        namespace_id: str,
        scope_key: str,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> MaintainResult:
        """按 UTC 日归档到期与普通证据超龄条目；不跳过无关待提取 Run。"""
        if fact_max_age_days < 1 or other_max_age_days < 1:
            raise ValueError("memory_maintenance_invalid")
        now = utc_now()
        fact_cutoff = now - timedelta(days=fact_max_age_days)
        other_cutoff = now - timedelta(days=other_max_age_days)
        async with self._factory() as session, session.begin():
            # 维护任务只锁定当前目标 scope 及其下属记忆行，不锁用户级 MemorySettingModel。
            state = await lock_scope_state(session, namespace_id, scope_key)
            if not state.enabled or state.last_maintained_day == day:
                return MaintainResult()
            expired_rows = (
                await session.scalars(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == namespace_id,
                        MemoryModel.scope_key == scope_key,
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.expires_at.is_not(None),
                        MemoryModel.expires_at <= now,
                    )
                    .order_by(MemoryModel.id)
                    .with_for_update()
                )
            ).all()
            for row in expired_rows:
                row.status = "archived"
                row.version += 1
                row.updated_at = now
            aged_rows = (
                await session.scalars(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == namespace_id,
                        MemoryModel.scope_key == scope_key,
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.explicit_long_term.is_not(True),
                        MemoryModel.last_evidenced_at.is_not(None),
                        or_(
                            (
                                (MemoryModel.kind == "fact")
                                & (MemoryModel.last_evidenced_at < fact_cutoff)
                            ),
                            (
                                (MemoryModel.kind != "fact")
                                & (MemoryModel.last_evidenced_at < other_cutoff)
                            ),
                        ),
                    )
                    .order_by(MemoryModel.id)
                    .with_for_update()
                )
            ).all()
            for row in aged_rows:
                row.status = "archived"
                row.version += 1
                row.updated_at = now
            state.last_maintained_day = day
            state.updated_at = now
        return MaintainResult(expired=len(expired_rows), age_archived=len(aged_rows))

    async def cleanup_scope(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> CleanupResult:
        """清除禁用 scope 的记忆正文；控制回执与扫描游标保留。"""
        cleared_memories = 0
        now = utc_now()
        async with self._factory() as session, session.begin():
            setting = await lock_user_memory_setting(session, namespace_id)
            state = await lock_scope_state(session, namespace_id, scope_key)
            if state.enabled:
                return CleanupResult()
            rows = (
                await session.scalars(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == namespace_id,
                        MemoryModel.scope_key == scope_key,
                        MemoryModel.deleted_time.is_(None),
                    )
                    .order_by(MemoryModel.id)
                    .with_for_update()
                )
            ).all()
            for row in rows:
                await self._tombstone(session, row, now)
                cleared_memories += 1
            project_id = (
                scope_key.removeprefix("project:") if scope_key.startswith("project:") else None
            )
            await self._mark_project_runs_skipped_locked(
                session,
                namespace_id,
                project_id,
                "memory_scope_disabled",
                now,
            )
            setting.generation += 1
        return CleanupResult(cleared_memories=cleared_memories)

    async def _mark_project_runs_skipped_locked(
        self,
        session: AsyncSession,
        namespace_id: str,
        project_id: str | None,
        safe_error_code: str,
        now: datetime,
    ) -> None:
        """停止一个项目来源相关的 pending 或尚未终态 Run。"""
        conditions: list[Any] = [
            RunModel.owner_id == namespace_id,
            RunModel.memory_project_id == project_id,
            or_(
                RunModel.memory_extraction_status.is_(None),
                RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                RunModel.finished_at.is_(None),
            ),
        ]
        rows = (
            await session.scalars(
                select(RunModel).where(*conditions).order_by(RunModel.id).with_for_update()
            )
        ).all()
        for row in rows:
            if row.memory_extraction_status in {
                None,
                ExtractionStatus.PENDING.value,
            }:
                row.memory_extraction_status = ExtractionStatus.SKIPPED.value
                row.memory_extraction_safe_error_code = safe_error_code
            if row.finished_at is not None:
                row.memory_extracted_at = now
            row.updated_at = now

    async def disable_scope_and_enqueue_cleanup(
        self,
        namespace_id: str,
        scope_key: str,
        *,
        disabled_at: datetime | None = None,
    ) -> int:
        """禁用 scope、推进 generation 并创建幂等清理任务。"""
        now = disabled_at or utc_now()
        async with self._factory() as session, session.begin():
            setting = await lock_user_memory_setting(session, namespace_id)
            state = await lock_scope_state(session, namespace_id, scope_key)
            state.enabled = False
            state.updated_at = now
            setting.generation += 1
            project_id = (
                scope_key.removeprefix("project:") if scope_key.startswith("project:") else None
            )
            await self._mark_project_runs_skipped_locked(
                session,
                namespace_id,
                project_id,
                "memory_scope_disabled",
                now,
            )
            await enqueue_cleanup_job(
                session,
                namespace_id=namespace_id,
                scope_key=scope_key,
                now=now,
            )
            return setting.generation
