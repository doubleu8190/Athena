"""记忆生命周期仓储：检索与上下文复核子模块。"""

from __future__ import annotations

from typing import Literal, cast

from sqlalchemy import Text, case, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from adapter.memory.converter import memory_to_domain
from adapter.memory.model import MemoryModel, MemorySettingModel
from adapter.memory.repository._shared import _MemoryRepositoryHelpersMixin
from common.date import as_utc
from domain.memory.lifecycle import (
    MemoryContextItem,
    MemoryContextSnapshot,
    MemoryReferenceKey,
    MemoryReferenceSnapshot,
)
from domain.memory.model import AccessContext, Memory, RetrievedMemory

_KIND_ORDER = {
    "constraint": 0,
    "decision": 1,
    "preference": 2,
    "fact": 3,
}


def _retrieved_memory(row: MemoryModel) -> RetrievedMemory:
    """把检索行投影为模型可见的固定 ID、版本和正文快照。"""
    memory = memory_to_domain(row)
    sources = tuple(
        sorted(
            memory.sources,
            key=lambda item: (
                item.source_at is None,
                item.source_at or memory.created_at,
                item.kind,
                item.reference,
            ),
        )
    )
    return RetrievedMemory(
        id=memory.id,
        namespace_id=memory.namespace_id,
        scope_key=memory.scope_key,
        content=memory.content,
        version=memory.version,
        status=cast(Literal["active", "archived"], memory.status),
        kind=memory.kind,
        load_policy=memory.load_policy,
        expires_at=memory.expires_at,
        updated_at=memory.updated_at,
        sources=sources,
    )


class MemoryRetrievalRepositoryMixin(_MemoryRepositoryHelpersMixin):
    """负责首轮上下文装配、发送前复核以及候选混合检索。"""

    async def list_context(
        self,
        access: AccessContext,
        scope_key: str,
        *,
        limit: int,
    ) -> tuple[Memory, ...]:
        """只读取当前 scope 的有效 always 条目，供首轮装配。"""
        if scope_key not in access.readable_scopes or not 1 <= limit <= 200:
            raise ValueError("memory_context_invalid")
        async with self._factory() as session:
            setting = await session.get(MemorySettingModel, access.namespace_id)
            if setting is not None and not setting.read_enabled:
                return ()
            rows = (
                await session.scalars(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == access.namespace_id,
                        MemoryModel.scope_key == scope_key,
                        MemoryModel.status == "active",
                        MemoryModel.load_policy == "always",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
                        self._enabled_scope_exists(
                            access.namespace_id,
                            scope_key,
                        ),
                    )
                    .order_by(
                        MemoryModel.explicit_long_term.desc(),
                        case(
                            _KIND_ORDER,
                            value=MemoryModel.kind,
                            else_=len(_KIND_ORDER),
                        ),
                        MemoryModel.last_evidenced_at.desc(),
                        MemoryModel.id,
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(memory_to_domain(row) for row in rows)

    async def revalidate_context(
        self,
        access: AccessContext,
        memories: tuple[tuple[str, int, str], ...],
    ) -> MemoryContextSnapshot:
        """按固定 ID/version/scope 在发送前复核读取开关和有效性。"""
        async with self._factory() as session:
            setting = await session.get(MemorySettingModel, access.namespace_id)
            enabled = setting.read_enabled if setting is not None else True
            if not enabled or not memories:
                return MemoryContextSnapshot(enabled, {})
            allowed = {
                memory_id: (version, scope)
                for memory_id, version, scope in memories
                if scope in access.readable_scopes
            }
            if not allowed:
                return MemoryContextSnapshot(enabled, {})
            rows = (
                await session.scalars(
                    select(MemoryModel).where(
                        MemoryModel.id.in_(tuple(allowed)),
                        MemoryModel.namespace_id == access.namespace_id,
                        MemoryModel.scope_key.in_(tuple({scope for _, scope in allowed.values()})),
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
                        self._enabled_memory_scope(),
                    )
                )
            ).all()
        items: dict[str, MemoryContextItem] = {}
        for row in rows:
            expected = allowed.get(row.id)
            if expected is None or row.version != expected[0] or row.scope_key != expected[1]:
                continue
            memory = memory_to_domain(row)
            items[row.id] = MemoryContextItem(
                memory_id=memory.id,
                version=memory.version,
                scope_key=memory.scope_key,
                content=memory.content,
                kind=memory.kind,
                load_policy=memory.load_policy,
                expires_at=memory.expires_at,
            )
        return MemoryContextSnapshot(enabled, items)

    async def revalidate_references(
        self,
        access: AccessContext,
        references: tuple[MemoryReferenceKey, ...],
    ) -> MemoryReferenceSnapshot:
        """按历史工具结果的精确 ID/version/scope 批量复核正文。"""
        requested = {
            reference for reference in references if reference[2] in access.readable_scopes
        }
        async with self._factory() as session:
            setting = await session.get(MemorySettingModel, access.namespace_id)
            enabled = setting.read_enabled if setting is not None else True
            if not enabled or not requested:
                return MemoryReferenceSnapshot(enabled, {})
            rows = (
                await session.scalars(
                    select(MemoryModel).where(
                        MemoryModel.id.in_({item[0] for item in requested}),
                        MemoryModel.namespace_id == access.namespace_id,
                        MemoryModel.scope_key.in_({item[2] for item in requested}),
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
                        self._enabled_memory_scope(),
                    )
                )
            ).all()
        items: dict[MemoryReferenceKey, MemoryContextItem] = {}
        for row in rows:
            key = (row.id, row.version, row.scope_key)
            if key not in requested:
                continue
            memory = memory_to_domain(row)
            items[key] = MemoryContextItem(
                memory_id=memory.id,
                version=memory.version,
                scope_key=memory.scope_key,
                content=memory.content,
                kind=memory.kind,
                load_policy=memory.load_policy,
                expires_at=memory.expires_at,
            )
        return MemoryReferenceSnapshot(enabled, items)

    async def retrieve_similar(
        self,
        access: AccessContext,
        scope_key: str,
        query: str,
        embedding: tuple[float, ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[RetrievedMemory, ...]:
        """在单一授权 scope 内融合全文与向量候选，供批量决策使用。"""
        results = await self.retrieve_similar_batch(
            access,
            ((scope_key, query, embedding),),
            embedding_profile_id,
            limit,
        )
        return results[0]

    async def retrieve_similar_batch(
        self,
        access: AccessContext,
        queries: tuple[tuple[str, str, tuple[float, ...]], ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[tuple[RetrievedMemory, ...], ...]:
        """在单个 REPEATABLE READ 事务内批量融合全文与向量候选，避免 N 次事务开销。"""
        if not embedding_profile_id.strip() or limit < 1:
            raise ValueError("memory_retrieval_invalid")
        normalized_queries: list[tuple[str, str, tuple[float, ...]]] = []
        for scope_key, query, embedding in queries:
            normalized_query = query.strip()
            if (
                scope_key not in access.readable_scopes
                or not normalized_query
                or len(normalized_query) > 2000
                or len(embedding) != 1024
            ):
                raise ValueError("memory_retrieval_invalid")
            normalized_queries.append((scope_key, normalized_query, embedding))
        if not normalized_queries:
            return ()
        effective_limit = min(limit, 10)
        results: list[tuple[RetrievedMemory, ...]] = []
        # 整批候选共享同一 REPEATABLE READ 快照事务，保证跨候选的旧记忆版本一致性。
        async with self._factory() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            setting = await session.get(MemorySettingModel, access.namespace_id)
            if setting is not None and not setting.read_enabled:
                return tuple(() for _ in normalized_queries)
            for scope_key, normalized_query, embedding in normalized_queries:
                results.append(
                    await self._retrieve_similar_in_session(
                        session,
                        access=access,
                        scope_key=scope_key,
                        normalized_query=normalized_query,
                        embedding=embedding,
                        embedding_profile_id=embedding_profile_id,
                        effective_limit=effective_limit,
                    )
                )
        return tuple(results)

    async def _retrieve_similar_in_session(
        self,
        session: AsyncSession,
        *,
        access: AccessContext,
        scope_key: str,
        normalized_query: str,
        embedding: tuple[float, ...],
        embedding_profile_id: str,
        effective_limit: int,
    ) -> tuple[RetrievedMemory, ...]:
        """在既有只读快照事务内执行单条候选的全文 + 向量混合检索。"""
        lexemes = func.tsvector_to_array(
            func.to_tsvector("athena_memory_chinese", normalized_query)
        )
        query_terms = func.unnest(lexemes).table_valued("lexeme").render_derived(name="query_terms")
        safe_terms = func.plainto_tsquery(
            "athena_memory_chinese",
            query_terms.c.lexeme,
        ).cast(Text)
        tsquery = (
            select(func.string_agg(safe_terms, " | ").cast(TSQUERY))
            .select_from(query_terms)
            .scalar_subquery()
        )
        keyword_rank = func.ts_rank_cd(MemoryModel.search_document, tsquery)
        vector_distance = MemoryModel.embedding.cosine_distance(list(embedding))
        common_conditions = (
            MemoryModel.namespace_id == access.namespace_id,
            MemoryModel.scope_key == scope_key,
            MemoryModel.status.in_(("active", "archived")),
            MemoryModel.deleted_time.is_(None),
            MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now()),
            self._enabled_memory_scope(),
        )
        keyword_rows = (
            await session.execute(
                select(MemoryModel, keyword_rank)
                .options(selectinload(MemoryModel.sources))
                .where(
                    *common_conditions,
                    MemoryModel.search_document.op("@@")(tsquery),
                )
                .order_by(keyword_rank.desc(), MemoryModel.id)
                .limit(effective_limit)
            )
        ).all()
        vector_rows = (
            await session.execute(
                select(MemoryModel, vector_distance)
                .options(selectinload(MemoryModel.sources))
                .where(
                    *common_conditions,
                    MemoryModel.embedding.is_not(None),
                    MemoryModel.embedding_profile_id == embedding_profile_id,
                )
                .order_by(vector_distance, MemoryModel.id)
                .limit(effective_limit)
            )
        ).all()

        rows_by_id: dict[str, MemoryModel] = {}
        versions_by_id: dict[str, int] = {}
        scores_by_id: dict[str, float] = {}
        version_conflicts: set[str] = set()
        for rows in (keyword_rows, vector_rows):
            for rank, row in enumerate(rows, start=1):
                memory = row[0]
                previous_version = versions_by_id.get(memory.id)
                if previous_version is not None and previous_version != memory.version:
                    version_conflicts.add(memory.id)
                    continue
                versions_by_id[memory.id] = memory.version
                rows_by_id[memory.id] = memory
                scores_by_id[memory.id] = scores_by_id.get(memory.id, 0.0) + (1.0 / (60 + rank))
        ordered_ids = sorted(
            (memory_id for memory_id in rows_by_id if memory_id not in version_conflicts),
            key=lambda memory_id: (
                -scores_by_id[memory_id],
                rows_by_id[memory_id].status != "active",
                -as_utc(rows_by_id[memory_id].updated_at).timestamp(),
                memory_id,
            ),
        )
        return tuple(
            _retrieved_memory(rows_by_id[memory_id]) for memory_id in ordered_ids[:effective_limit]
        )
