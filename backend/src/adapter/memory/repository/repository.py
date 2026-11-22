"""PostgreSQL 记忆仓储门面：组合检索、提取、控制、任务调度与来源读取子仓储。"""

from __future__ import annotations

from builtins import list as List
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Text, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from adapter.memory.converter import memory_to_domain
from adapter.memory.model import MemoryModel, MemorySettingModel
from adapter.memory.repository._shared import (
    _mark_user_runs_skipped_locked,
    enabled_memory_scope,
    lock_user_memory_setting,
)
from adapter.memory.repository.batch_extract import MemoryBatchExtractRepository
from adapter.memory.repository.control import MemoryControlRepository
from adapter.memory.repository.control_source import MemoryControlSourceRepository
from adapter.memory.repository.jobs import MemoryJobsRepository
from adapter.memory.repository.retrieval import MemoryRetrievalRepository
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    CleanupResult,
    CompleteJobCommand,
    ControlCommand,
    ControlReceiptResult,
    ControlSourceSnapshot,
    ExtractBatchPayload,
    ExtractBatchResult,
    JobKind,
    MaintainResult,
    MemoryContextSnapshot,
    MemoryJob,
    MemoryPendingRun,
    MemoryReferenceKey,
    MemoryReferenceSnapshot,
    MemoryScopeState,
    MemorySourceMessage,
    ScanResult,
)
from domain.memory.model import AccessContext, Memory, RecallSnapshot, RetrievedMemory
from domain.memory.ports import MemoryControlSourcePort, MemoryRepositoryPort


class MemoryRepository(MemoryRepositoryPort, MemoryControlSourcePort):
    """封装记忆自有表的事务与授权范围查询的统一组合门面。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._retrieval = MemoryRetrievalRepository(factory)
        self._batch_extract = MemoryBatchExtractRepository(factory)
        self._control = MemoryControlRepository(factory)
        self._jobs = MemoryJobsRepository(factory)
        self._control_source = MemoryControlSourceRepository(factory)

    async def settings(self, namespace_id: str) -> tuple[bool, bool]:
        async with self._factory() as session:
            row = await session.get(MemorySettingModel, namespace_id)
        return (row.read_enabled, row.auto_extract_enabled) if row else (True, True)

    async def update_settings(
        self, namespace_id: str, read_enabled: bool, auto_extract_enabled: bool
    ) -> None:
        async with self._factory() as session, session.begin():
            now = datetime.now(UTC)
            setting = await lock_user_memory_setting(session, namespace_id)
            if (
                setting.read_enabled == read_enabled
                and setting.auto_extract_enabled == auto_extract_enabled
            ):
                return
            setting.read_enabled = read_enabled
            setting.auto_extract_enabled = auto_extract_enabled
            setting.generation += 1
            if not auto_extract_enabled:
                await _mark_user_runs_skipped_locked(
                    session,
                    owner_id=namespace_id,
                    safe_error_code="memory_auto_extract_disabled",
                    now=now,
                )

    async def search_keyword(
        self, namespace: str, scopes: tuple[str, ...], query: str, limit: int
    ) -> list[tuple[Memory, float]]:
        if not scopes or limit < 1:
            return []
        terms = [term for term in query.strip().split() if term]
        if not terms:
            return []
        normalized_query = " ".join(terms[:8])
        async with self._factory() as session:
            read_enabled, _ = await self._settings_in_session(session, namespace)
            if not read_enabled:
                return []
            # 使用 to_tsvector + plainto_tsquery 安全构造 tsquery，
            # 禁止将原始符号直接喂给 to_tsquery；空词元时 string_agg 返回 NULL，安全命中 0 行。
            lexemes = func.tsvector_to_array(
                func.to_tsvector("athena_memory_chinese", normalized_query)
            )
            query_terms = (
                func.unnest(lexemes).table_valued("lexeme").render_derived(name="query_terms")
            )
            safe_terms = func.nullif(
                func.plainto_tsquery(
                    "athena_memory_chinese",
                    query_terms.c.lexeme,
                ).cast(Text),
                "",
            )
            tsquery = (
                select(func.string_agg(safe_terms, " & ").cast(TSQUERY))
                .select_from(query_terms)
                .scalar_subquery()
            )
            score = func.ts_rank(MemoryModel.search_document, tsquery)
            rows = (
                (
                    await session.execute(
                        select(MemoryModel, score.label("score"))
                        .options(joinedload(MemoryModel.sources))
                        .where(
                            MemoryModel.namespace_id == namespace,
                            MemoryModel.scope_key.in_(scopes),
                            MemoryModel.status == "active",
                            MemoryModel.deleted_time.is_(None),
                            # 排除已过期的记忆，与检索复核子模块保持一致。
                            MemoryModel.expires_at.is_(None)
                            | (MemoryModel.expires_at > func.now()),
                            enabled_memory_scope(),
                            MemoryModel.search_document.op("@@")(tsquery),
                        )
                        .order_by(score.desc(), MemoryModel.updated_at.desc(), MemoryModel.id)
                        .limit(limit)
                    )
                )
                .unique()
                .all()
            )
            return [
                (
                    memory_to_domain(cast(MemoryModel, row)),
                    float(cast(float | None, row_score) or 0.0),
                )
                for row, row_score in rows
            ]

    async def search_vector(
        self, namespace: str, scopes: tuple[str, ...], vector: List[float], profile: str, limit: int
    ) -> list[tuple[Memory, float]]:
        if not scopes or limit < 1:
            return []
        async with self._factory() as session:
            read_enabled, _ = await self._settings_in_session(session, namespace)
            if not read_enabled:
                return []
            distance = MemoryModel.embedding.cosine_distance(vector)
            rows = (
                (
                    await session.execute(
                        select(MemoryModel, (1 - distance).label("score"))
                        .options(joinedload(MemoryModel.sources))
                        .where(
                            MemoryModel.namespace_id == namespace,
                            MemoryModel.scope_key.in_(scopes),
                            MemoryModel.status == "active",
                            MemoryModel.deleted_time.is_(None),
                            # 排除已过期的记忆，与检索复核子模块保持一致。
                            MemoryModel.expires_at.is_(None)
                            | (MemoryModel.expires_at > func.now()),
                            enabled_memory_scope(),
                            MemoryModel.embedding_profile_id == profile,
                            MemoryModel.embedding.is_not(None),
                        )
                        .order_by(distance, MemoryModel.updated_at.desc(), MemoryModel.id)
                        .limit(limit)
                    )
                )
                .unique()
                .all()
            )
            return [
                (
                    memory_to_domain(cast(MemoryModel, row)),
                    float(cast(float | None, row_score) or 0.0),
                )
                for row, row_score in rows
            ]

    async def get_many(
        self, ids: tuple[str, ...], namespace: str, scopes: tuple[str, ...]
    ) -> RecallSnapshot:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            read_enabled, _ = await self._settings_in_session(session, namespace)
            if not read_enabled:
                return RecallSnapshot(False, {})
            if not ids or not scopes:
                return RecallSnapshot(True, {})
            rows = (
                (
                    await session.scalars(
                        select(MemoryModel)
                        .options(joinedload(MemoryModel.sources))
                        .where(
                            MemoryModel.id.in_(ids),
                            MemoryModel.namespace_id == namespace,
                            MemoryModel.scope_key.in_(scopes),
                            MemoryModel.status == "active",
                            MemoryModel.deleted_time.is_(None),
                            enabled_memory_scope(),
                        )
                    )
                )
                .unique()
                .all()
            )
            memories = {row.id: memory_to_domain(row) for row in rows}
            return RecallSnapshot(True, memories)

    @staticmethod
    async def _settings_in_session(session: AsyncSession, namespace_id: str) -> tuple[bool, bool]:
        row = await session.get(MemorySettingModel, namespace_id)
        return (row.read_enabled, row.auto_extract_enabled) if row else (True, True)

    async def list_context(
        self,
        access: AccessContext,
        scope_key: str,
        *,
        limit: int,
    ) -> tuple[Memory, ...]:
        return await self._retrieval.list_context(access, scope_key, limit=limit)

    async def revalidate_context(
        self,
        access: AccessContext,
        memories: tuple[tuple[str, int, str], ...],
    ) -> MemoryContextSnapshot:
        return await self._retrieval.revalidate_context(access, memories)

    async def revalidate_references(
        self,
        access: AccessContext,
        references: tuple[MemoryReferenceKey, ...],
    ) -> MemoryReferenceSnapshot:
        return await self._retrieval.revalidate_references(access, references)

    async def retrieve_similar_batch(
        self,
        access: AccessContext,
        queries: tuple[tuple[str, str, tuple[float, ...]], ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[tuple[RetrievedMemory, ...], ...]:
        return await self._retrieval.retrieve_similar_batch(
            access, queries, embedding_profile_id, limit
        )

    async def apply_batch(self, command: ApplyBatchCommand) -> ExtractBatchResult:
        return await self._batch_extract.apply_batch(command)

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult:
        return await self._control.apply_control(command)

    async def control_receipt(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        arguments_digest: str,
    ) -> ControlReceiptResult | None:
        return await self._control.control_receipt(
            run_id=run_id, tool_call_id=tool_call_id, arguments_digest=arguments_digest
        )

    async def scope_state(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> MemoryScopeState:
        return await self._control.scope_state(namespace_id, scope_key)

    async def wake_user(self, owner_id: str, *, respect_backoff: bool = False) -> None:
        await self._jobs.wake_user(owner_id, respect_backoff=respect_backoff)

    async def user_generation(self, owner_id: str) -> int:
        return await self._control.user_generation(owner_id)

    async def recover_running_jobs(self) -> int:
        return await self._jobs.recover_running_jobs()

    async def claim_job(
        self,
        *,
        kinds: tuple[JobKind, ...],
        limit: int = 1,
    ) -> MemoryJob | None:
        return await self._jobs.claim_job(kinds=kinds, limit=limit)

    async def complete_job(self, command: CompleteJobCommand) -> bool:
        return await self._jobs.complete_job(command)

    async def list_pending_runs(
        self,
        owner_id: str,
        *,
        limit: int,
        token_budget: int,
    ) -> tuple[MemoryPendingRun, ...]:
        return await self._batch_extract.list_pending_runs(
            owner_id, limit=limit, token_budget=token_budget
        )

    async def load_batch_sources(
        self,
        batch: ExtractBatchPayload,
    ) -> tuple[MemorySourceMessage, ...]:
        return await self._batch_extract.load_batch_sources(batch)

    async def skip_runs(
        self,
        owner_id: str,
        run_ids: tuple[str, ...],
        safe_error_code: str,
    ) -> int:
        return await self._batch_extract.skip_runs(owner_id, run_ids, safe_error_code)

    async def list_all_pending_runs(
        self,
        *,
        after_finished_at: datetime | None,
        after_run_id: str | None,
        limit: int,
    ) -> tuple[MemoryPendingRun, ...]:
        return await self._batch_extract.list_all_pending_runs(
            after_finished_at=after_finished_at,
            after_run_id=after_run_id,
            limit=limit,
        )

    async def scan_missing_runs(
        self,
        *,
        enabled_boundary_at: datetime,
        cursor_finished_at: datetime | None = None,
        cursor_run_id: str | None = None,
        page_limit: int = 100,
    ) -> ScanResult:
        return await self._jobs.scan_missing_runs(
            enabled_boundary_at=enabled_boundary_at,
            cursor_finished_at=cursor_finished_at,
            cursor_run_id=cursor_run_id,
            page_limit=page_limit,
        )

    async def maintain_due_scopes(
        self,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> int:
        return await self._jobs.maintain_due_scopes(
            day,
            fact_max_age_days=fact_max_age_days,
            other_max_age_days=other_max_age_days,
        )

    async def maintain_scope(
        self,
        namespace_id: str,
        scope_key: str,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> MaintainResult:
        return await self._jobs.maintain_scope(
            namespace_id,
            scope_key,
            day,
            fact_max_age_days=fact_max_age_days,
            other_max_age_days=other_max_age_days,
        )

    async def cleanup_scope(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> CleanupResult:
        return await self._jobs.cleanup_scope(namespace_id, scope_key)

    async def read_control_source(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_id: str,
    ) -> ControlSourceSnapshot | None:
        return await self._control_source.read_control_source(
            owner_id=owner_id,
            session_id=session_id,
            run_id=run_id,
        )
