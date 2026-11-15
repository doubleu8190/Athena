"""记忆生命周期仓储：异步批次提交与待提取 Run 子模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.memory.model import (
    MemoryJobModel,
    MemoryModel,
    MemoryScopeStateModel,
    MemorySettingModel,
)
from adapter.memory.repository._shared import (
    MemoryBatchDiscarded,
    _job_result,
    _json_value,
    _mark_batch_runs_locked,
    _mark_user_runs_skipped_locked,
    _MemoryRepositoryHelpersMixin,
    _message_text,
    _scope_key,
    _source_message,
    lock_user_memory_setting,
    read_user_memory_generation,
)
from adapter.message.model import ConversationMessageModel
from adapter.project.model import ProjectModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from common.date import as_utc, utc_now
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    DecisionAction,
    ExtractBatchPayload,
    ExtractBatchResult,
    ExtractionStatus,
    ExtractJobResult,
    JobKind,
    JobStatus,
    MemoryPendingRun,
    MemorySourceMessage,
    RunMemoryContextState,
)
from domain.message import ConversationRole


class MemoryBatchExtractRepositoryMixin(_MemoryRepositoryHelpersMixin):
    """负责待提取 Run 扫描、来源加载与批次原子提交。"""

    async def apply_batch(self, command: ApplyBatchCommand) -> ExtractBatchResult:
        """单事务提交整批记忆变化、Run 完成和任务完成。"""
        batch = command.batch
        now = utc_now()
        async with self._factory() as session, session.begin():
            setting = await lock_user_memory_setting(session, batch.owner_id)
            job = await session.scalar(
                select(MemoryJobModel).where(MemoryJobModel.id == command.job_id).with_for_update()
            )
            if job is None or job.kind != JobKind.EXTRACT_PENDING.value:
                raise ValueError("memory_batch_job_invalid")
            replay = self._extract_replay_result(job, batch.batch_id)
            if replay is not None:
                return replay
            if job.status != JobStatus.RUNNING.value or job.namespace_id != batch.owner_id:
                raise ValueError("memory_batch_job_changed")

            if command.discard_reason is not None:
                if (
                    command.changes
                    or command.summarized_skips != 0
                    or not command.discard_reason.startswith("memory_")
                    or len(command.discard_reason) > 128
                ):
                    raise ValueError("memory_batch_discard_invalid")
                return await self._discard_batch_locked(
                    session,
                    setting,
                    job,
                    batch,
                    reason=command.discard_reason,
                    now=now,
                )
            if not setting.auto_extract_enabled:
                return await self._discard_batch_locked(
                    session,
                    setting,
                    job,
                    batch,
                    reason="memory_auto_extract_disabled",
                    now=now,
                )

            if not batch.extraction_complete or not batch.decisions_complete:
                raise ValueError("memory_batch_decisions_incomplete")
            if command.summarized_skips != batch.skipped:
                raise ValueError("memory_batch_skip_changed")

            try:
                allowed_sources, _scopes = await self._lock_batch_context(
                    session,
                    batch,
                )
            except MemoryBatchDiscarded as error:
                return await self._discard_batch_locked(
                    session,
                    setting,
                    job,
                    batch,
                    reason=str(error),
                    now=now,
                )
            created = updated = ignored = 0
            skipped = command.summarized_skips
            for change in command.changes:
                allowed = allowed_sources.get(change.scope_key, {})
                if any(item.message_id not in allowed for item in change.source_evidence):
                    raise MemoryBatchDiscarded("memory_batch_source_invalid")
                if change.action is DecisionAction.ADD:
                    create = change.create
                    if create is None:
                        raise ValueError("memory_batch_plan_invalid")
                    if await self._insert_prepared(session, create, now) is False:
                        raise ValueError("memory_batch_create_replayed")
                    created += 1
                elif change.action is DecisionAction.UPDATE:
                    create = change.create
                    if create is None:
                        raise ValueError("memory_batch_plan_invalid")
                    # 通过行级 version 乐观锁保护 UPDATE 目标，不再依赖整批 generation 作废。
                    target = await self._lock_target_memory(
                        session,
                        change.namespace_id,
                        change.scope_key,
                        change.target_memory_id,
                        change.target_version,
                    )
                    if await self._insert_prepared(session, create, now) is False:
                        raise ValueError("memory_batch_create_replayed")
                    await self._copy_evidence_sources(
                        session,
                        source_memory_id=target.id,
                        target_memory_id=create.memory_id,
                    )
                    await self._preserve_replacement_evidence_time(
                        session,
                        memory_id=create.memory_id,
                        create=create,
                        target=target,
                    )
                    target.status = "superseded"
                    target.superseded_by_id = create.memory_id
                    target.version += 1
                    target.updated_at = now
                    updated += 1
                elif change.action is DecisionAction.IGNORE:
                    ignored += 1
                else:
                    raise ValueError("memory_batch_action_invalid")

            await _mark_batch_runs_locked(
                session,
                batch,
                outcome=ExtractionStatus.COMPLETED,
                safe_error_code=None,
                now=now,
            )
            setting.generation += 1
            result = ExtractBatchResult(
                batch_id=batch.batch_id,
                run_ids=tuple(item.run_id for item in batch.members),
                created=created,
                updated=updated,
                ignored=ignored,
                skipped=skipped,
            )
            job.result = _json_value(ExtractJobResult(result=result))
            job.attempt_count = 0
            job.schema_retry_count = 0
            job.available_at = now
            job.updated_at = now
            job.status = (
                JobStatus.QUEUED.value
                if await self._owner_has_pending_runs(session, batch.owner_id)
                else JobStatus.DONE.value
            )
        return result

    @staticmethod
    def _extract_replay_result(
        job: MemoryJobModel,
        batch_id: str,
    ) -> ExtractBatchResult | None:
        """读取同一批次已提交结果，保证提交后响应丢失不重跑。"""
        if job.result is None:
            return None
        try:
            result = _job_result(job.result)
        except (ValidationError, ValueError):
            return None
        if isinstance(result, ExtractJobResult) and result.result.batch_id == batch_id:
            return result.result
        return None

    async def _discard_batch_locked(
        self,
        session: AsyncSession,
        setting: MemorySettingModel,
        job: MemoryJobModel,
        batch: ExtractBatchPayload,
        *,
        reason: str,
        now: datetime,
    ) -> ExtractBatchResult:
        """不写记忆，仅整批收敛 Run 和用户任务。"""
        await _mark_batch_runs_locked(
            session,
            batch,
            outcome=ExtractionStatus.SKIPPED,
            safe_error_code=reason,
            now=now,
        )
        result = ExtractBatchResult(
            batch_id=batch.batch_id,
            run_ids=tuple(item.run_id for item in batch.members),
            status="discarded",
            discard_reason=reason,
        )
        job.result = _json_value(ExtractJobResult(result=result))
        job.attempt_count = 0
        job.schema_retry_count = 0
        job.available_at = now
        job.updated_at = now
        job.status = (
            JobStatus.QUEUED.value
            if await self._owner_has_pending_runs(session, setting.namespace_id)
            else JobStatus.DONE.value
        )
        return result

    async def list_pending_runs(
        self,
        owner_id: str,
        *,
        limit: int,
        token_budget: int,
    ) -> tuple[MemoryPendingRun, ...]:
        """读取一个用户的有界 pending Run，并记录读取时的用户 generation。"""
        if not owner_id.strip() or not 1 <= limit <= 20 or token_budget < 1:
            raise ValueError("memory_pending_query_invalid")
        now = utc_now()
        async with self._factory() as session, session.begin():
            setting = await lock_user_memory_setting(session, owner_id)
            if not setting.auto_extract_enabled:
                await _mark_user_runs_skipped_locked(
                    session,
                    owner_id=owner_id,
                    safe_error_code="memory_auto_extract_disabled",
                    now=now,
                )
                return ()
            rows = (
                await session.scalars(
                    select(RunModel)
                    .where(
                        RunModel.owner_id == owner_id,
                        RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                        RunModel.finished_at.is_not(None),
                    )
                    .order_by(RunModel.finished_at, RunModel.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            result: list[MemoryPendingRun] = []
            char_budget = token_budget * 4
            used = 0
            for row in rows:
                pending = await self._pending_run(
                    session,
                    row,
                    now,
                    generation=setting.generation,
                )
                if pending is None:
                    continue
                size = sum(len(item.text) for item in pending.messages)
                if result and used + size > char_budget:
                    break
                result.append(pending)
                used += size
            return tuple(result)

    async def skip_runs(
        self,
        owner_id: str,
        run_ids: tuple[str, ...],
        safe_error_code: str,
    ) -> int:
        """跳过明确超长或无有效证据的 Run，不进行分片。"""
        if (
            not owner_id.strip()
            or not run_ids
            or len(set(run_ids)) != len(run_ids)
            or not safe_error_code.startswith("memory_")
            or len(safe_error_code) > 128
        ):
            raise ValueError("memory_skip_runs_invalid")
        now = utc_now()
        async with self._factory() as session, session.begin():
            await lock_user_memory_setting(session, owner_id)
            rows = (
                await session.scalars(
                    select(RunModel)
                    .where(
                        RunModel.id.in_(run_ids),
                        RunModel.owner_id == owner_id,
                        RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                    )
                    .order_by(RunModel.id)
                    .with_for_update()
                )
            ).all()
            for row in rows:
                row.memory_extraction_status = ExtractionStatus.SKIPPED.value
                row.memory_extracted_at = now
                row.memory_extraction_safe_error_code = safe_error_code
                row.updated_at = now
            return len(rows)

    async def load_batch_sources(
        self,
        batch: ExtractBatchPayload,
    ) -> tuple[MemorySourceMessage, ...]:
        """模型调用前重新读取固定批次来源，并在短事务内复核可见性与自动提取开关。"""
        async with self._factory() as session, session.begin():
            setting = await lock_user_memory_setting(session, batch.owner_id)
            if not setting.auto_extract_enabled:
                raise MemoryBatchDiscarded("memory_auto_extract_disabled")
            await self._lock_batch_context(session, batch)
            source_ids = tuple(
                sorted(
                    {
                        source_id
                        for member in batch.members
                        for source_id in member.source_message_ids
                    }
                )
            )
            rows = (
                await session.scalars(
                    select(ConversationMessageModel)
                    .where(ConversationMessageModel.id.in_(source_ids))
                    .order_by(
                        ConversationMessageModel.created_at,
                        ConversationMessageModel.id,
                    )
                )
            ).all()
        messages = tuple(_source_message(row) for row in rows)
        if {item.message_id for item in messages} != set(source_ids) or any(
            not item.text.strip() for item in messages
        ):
            raise MemoryBatchDiscarded("memory_batch_source_changed")
        return messages

    async def _lock_batch_context(
        self,
        session: AsyncSession,
        batch: ExtractBatchPayload,
    ) -> tuple[dict[str, dict[str, datetime]], dict[str, MemoryScopeStateModel]]:
        """校验只读归属与历史依据存在性，并锁定需修改状态的 scope 与 Run。"""
        project_ids = {member.project_id for member in batch.members}
        if len(project_ids) != 1:
            raise MemoryBatchDiscarded("memory_batch_project_mixed")
        project_id = next(iter(project_ids))
        if project_id is not None:
            # ProjectModel 为只读归属校验表，不加 FOR UPDATE 行锁。
            project = await session.scalar(
                select(ProjectModel).where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == batch.owner_id,
                    ProjectModel.deleted_time.is_(None),
                )
            )
            if project is None:
                raise MemoryBatchDiscarded("memory_batch_project_unavailable")
        session_ids = tuple(sorted({item.session_id for item in batch.members}))
        # SessionModel 为只读归属校验表，不加 FOR UPDATE 行锁。
        sessions = (
            await session.scalars(
                select(SessionModel)
                .where(SessionModel.id.in_(session_ids))
                .order_by(SessionModel.id)
            )
        ).all()
        sessions_by_id = {item.id: item for item in sessions}
        for member in batch.members:
            source_session = sessions_by_id.get(member.session_id)
            if (
                source_session is None
                or source_session.deleted_time is not None
                or source_session.owner_id != batch.owner_id
                or source_session.project_id != member.project_id
            ):
                raise MemoryBatchDiscarded("memory_batch_session_changed")
        scope_rows = (
            await session.scalars(
                select(MemoryScopeStateModel)
                .where(
                    MemoryScopeStateModel.namespace_id == batch.owner_id,
                    MemoryScopeStateModel.scope_key.in_(batch.scope_keys),
                )
                .order_by(
                    MemoryScopeStateModel.namespace_id,
                    MemoryScopeStateModel.scope_key,
                )
                .with_for_update()
            )
        ).all()
        scopes = {row.scope_key: row for row in scope_rows}
        if any(
            scope_key not in scopes or not scopes[scope_key].enabled
            for scope_key in batch.scope_keys
        ):
            raise MemoryBatchDiscarded("memory_scope_disabled")
        run_ids = tuple(sorted(item.run_id for item in batch.members))
        runs = (
            await session.scalars(
                select(RunModel)
                .where(RunModel.id.in_(run_ids))
                .order_by(RunModel.id)
                .with_for_update()
            )
        ).all()
        runs_by_id = {item.id: item for item in runs}
        if set(runs_by_id) != set(run_ids):
            raise MemoryBatchDiscarded("memory_batch_member_changed")
        allowed_sources: dict[str, dict[str, datetime]] = {
            scope_key: {} for scope_key in batch.scope_keys
        }
        for member in batch.members:
            run = runs_by_id[member.run_id]
            if (
                run.owner_id != batch.owner_id
                or run.session_id != member.session_id
                or run.memory_project_id != member.project_id
                or run.memory_extraction_status != ExtractionStatus.PENDING.value
                or run.finished_at is None
            ):
                raise MemoryBatchDiscarded("memory_batch_member_changed")
            allowed_input_ids = set(run.memory_input_message_ids or (run.trigger_message_id,))
            if not set(member.source_message_ids).issubset(allowed_input_ids):
                raise MemoryBatchDiscarded("memory_batch_source_changed")
            member_scope = _scope_key(member.project_id)
            for source_id in member.source_message_ids:
                allowed_sources[member_scope][source_id] = member.source_cutoff_at
                if member.project_id is not None:
                    allowed_sources["personal"][source_id] = member.source_cutoff_at
        source_ids = tuple(
            sorted(
                {source_id for member in batch.members for source_id in member.source_message_ids}
            )
        )
        # ConversationMessageModel 为只读历史依据表，不加 FOR UPDATE 行锁。
        source_rows = (
            await session.scalars(
                select(ConversationMessageModel)
                .where(
                    ConversationMessageModel.id.in_(source_ids),
                    ConversationMessageModel.role == ConversationRole.USER.value,
                )
                .order_by(ConversationMessageModel.id)
            )
        ).all()
        sources_by_id = {item.id: item for item in source_rows}
        if set(sources_by_id) != set(source_ids):
            raise MemoryBatchDiscarded("memory_batch_source_changed")
        for member in batch.members:
            for source_id in member.source_message_ids:
                source = sources_by_id[source_id]
                if (
                    source.session_id != member.session_id
                    or not _message_text(source.parts).strip()
                ):
                    raise MemoryBatchDiscarded("memory_batch_source_changed")
                source_at = as_utc(source.completed_at or source.created_at)
                allowed_sources[_scope_key(member.project_id)][source_id] = source_at
                if member.project_id is not None:
                    allowed_sources["personal"][source_id] = source_at
        return allowed_sources, scopes

    async def list_all_pending_runs(
        self,
        *,
        after_finished_at: datetime | None,
        after_run_id: str | None,
        limit: int,
    ) -> tuple[MemoryPendingRun, ...]:
        """扫描全局 pending 页，使用 (finished_at,id) 游标。"""
        if not 1 <= limit <= 500:
            raise ValueError("memory_pending_query_invalid")
        now = utc_now()
        conditions: list[Any] = [
            RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
            RunModel.finished_at.is_not(None),
        ]
        if after_finished_at is not None:
            conditions.append(
                or_(
                    RunModel.finished_at > after_finished_at,
                    (RunModel.finished_at == after_finished_at)
                    & (RunModel.id > (after_run_id or "")),
                )
            )
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(RunModel)
                    .where(*conditions)
                    .order_by(RunModel.finished_at, RunModel.id)
                    .limit(limit)
                )
            ).all()
            result: list[MemoryPendingRun] = []
            for row in rows:
                pending = await self._pending_run(
                    session,
                    row,
                    now,
                    generation=await read_user_memory_generation(session, row.owner_id),
                )
                if pending is not None:
                    result.append(pending)
            return tuple(result)

    async def _pending_run(
        self,
        session: AsyncSession,
        row: RunModel,
        now: datetime,
        *,
        generation: int,
    ) -> MemoryPendingRun | None:
        """复核 Session/项目固定范围，并返回全部消息与固定上下文引用。"""
        session_row = await session.get(SessionModel, row.session_id)
        if (
            session_row is None
            or session_row.deleted_time is not None
            or session_row.owner_id != row.owner_id
            or session_row.project_id != row.memory_project_id
        ):
            row.memory_extraction_status = ExtractionStatus.SKIPPED.value
            row.memory_extracted_at = now
            row.memory_extraction_safe_error_code = "memory_source_scope_invalid"
            row.updated_at = now
            return None
        ids = tuple(row.memory_input_message_ids or (row.trigger_message_id,))
        message_rows = (
            await session.scalars(
                select(ConversationMessageModel)
                .where(
                    ConversationMessageModel.id.in_(ids),
                    ConversationMessageModel.session_id == row.session_id,
                    ConversationMessageModel.role == ConversationRole.USER.value,
                )
                .order_by(ConversationMessageModel.created_at, ConversationMessageModel.id)
            )
        ).all()
        messages = tuple(
            _source_message(item) for item in message_rows if _message_text(item.parts)
        )
        if len({item.message_id for item in messages}) != len(ids):
            row.memory_extraction_status = ExtractionStatus.SKIPPED.value
            row.memory_extracted_at = now
            row.memory_extraction_safe_error_code = "memory_source_unavailable"
            row.updated_at = now
            return None
        try:
            context_state = RunMemoryContextState.model_validate(row.memory_context_state or {})
        except ValidationError:
            context_state = RunMemoryContextState()
        return MemoryPendingRun(
            run_id=row.id,
            owner_id=row.owner_id,
            session_id=row.session_id,
            project_id=row.memory_project_id,
            finished_at=as_utc(row.finished_at or now),
            generation=generation,
            messages=messages,
            context_state=context_state,
        )

    async def _owner_has_pending_runs(
        self,
        session: AsyncSession,
        owner_id: str,
    ) -> bool:
        """判断用户任务完成后是否仍有待处理的 pending Run。"""
        setting = await session.get(MemorySettingModel, owner_id)
        if setting is not None and not setting.auto_extract_enabled:
            return False
        pending_id = await session.scalar(
            select(RunModel.id)
            .where(
                RunModel.owner_id == owner_id,
                RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
            )
            .limit(1)
        )
        return pending_id is not None

    async def _lock_target_memory(
        self,
        session: AsyncSession,
        namespace_id: str,
        scope_key: str,
        target_id: str | None,
        target_version: int | None,
    ) -> MemoryModel:
        """锁定可重新激活的未过期目标并复核行级乐观锁版本。"""
        if target_id is None or target_version is None:
            raise ValueError("memory_batch_target_invalid")
        row = await session.scalar(
            select(MemoryModel)
            .where(
                MemoryModel.id == target_id,
                MemoryModel.namespace_id == namespace_id,
                MemoryModel.scope_key == scope_key,
                MemoryModel.status.in_(("active", "archived")),
                MemoryModel.deleted_time.is_(None),
                MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
                MemoryModel.version == target_version,
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("memory_batch_target_changed")
        return row
