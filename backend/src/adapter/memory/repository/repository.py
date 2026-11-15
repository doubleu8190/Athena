"""PostgreSQL 记忆仓储与混合候选查询。"""

from __future__ import annotations

from builtins import list as List
from datetime import UTC, datetime

from sqlalchemy import Text, delete, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from adapter.memory.converter import memory_to_domain
from adapter.memory.model import MemoryModel, MemorySettingModel, MemorySourceModel
from adapter.memory.repository.lifecycle import (
    MemoryLifecycleRepositoryMixin,
    _mark_user_runs_skipped_locked,
    _normalized_content_hash,
    ensure_scan_job_locked,
    lock_scope_state,
    lock_user_memory_setting,
)
from domain.memory.model import AccessContext, Memory, RecallSnapshot


class MemoryRepository(MemoryLifecycleRepositoryMixin):
    """封装记忆自有表的事务和授权范围查询。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

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
            was_auto_extract_enabled = setting.auto_extract_enabled
            setting.read_enabled = read_enabled
            setting.auto_extract_enabled = auto_extract_enabled
            setting.generation += 1
            if not auto_extract_enabled:
                # 仅在关闭自动提取时跳过该用户尚未处理的待提取 Run。
                await _mark_user_runs_skipped_locked(
                    session,
                    owner_id=namespace_id,
                    safe_error_code="memory_auto_extract_disabled",
                    now=now,
                )
            if auto_extract_enabled and not was_auto_extract_enabled:
                # 首次开启只处理边界之后的新 Run。
                await ensure_scan_job_locked(session, now)

    async def search_keyword(
        self, namespace: str, scopes: tuple[str, ...], query: str, limit: int
    ) -> List[tuple[Memory, float]]:
        lexemes = func.tsvector_to_array(func.to_tsvector("athena_memory_chinese", query))
        query_terms = func.unnest(lexemes).table_valued("lexeme").render_derived(name="query_terms")
        safe_terms = func.plainto_tsquery("athena_memory_chinese", query_terms.c.lexeme).cast(Text)
        tsquery = (
            select(func.string_agg(safe_terms, " | ").cast(TSQUERY))
            .select_from(query_terms)
            .scalar_subquery()
        )
        rank = func.ts_rank_cd(MemoryModel.search_document, tsquery)
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(MemoryModel, rank)
                    .where(
                        MemoryModel.namespace_id == namespace,
                        MemoryModel.scope_key.in_(scopes),
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        (MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now())),
                        MemoryModel.search_document.op("@@")(tsquery),
                        self._enabled_memory_scope(),
                    )
                    .order_by(rank.desc(), MemoryModel.id)
                    .limit(limit)
                )
            ).all()
            return [(memory_to_domain(row[0]), float(row[1])) for row in rows]

    async def search_vector(
        self, namespace: str, scopes: tuple[str, ...], vector: list[float], profile: str, limit: int
    ) -> List[tuple[Memory, float]]:
        distance = MemoryModel.embedding.cosine_distance(vector)
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(MemoryModel, distance)
                    .where(
                        MemoryModel.namespace_id == namespace,
                        MemoryModel.scope_key.in_(scopes),
                        MemoryModel.status == "active",
                        MemoryModel.deleted_time.is_(None),
                        MemoryModel.embedding.is_not(None),
                        MemoryModel.embedding_profile_id == profile,
                        (MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > func.now())),
                        self._enabled_memory_scope(),
                    )
                    .order_by(distance, MemoryModel.id)
                    .limit(limit)
                )
            ).all()
            return [(memory_to_domain(row[0]), 1.0 - float(row[1])) for row in rows]

    async def get_many(
        self, ids: tuple[str, ...], namespace: str, scopes: tuple[str, ...]
    ) -> RecallSnapshot:
        async with self._factory() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            setting = await session.get(MemorySettingModel, namespace)
            enabled = setting.read_enabled if setting is not None else True
            if not ids:
                return RecallSnapshot(enabled, {})
            rows = (
                (
                    await session.execute(
                        select(MemoryModel)
                        .options(joinedload(MemoryModel.sources))
                        .where(
                            MemoryModel.id.in_(ids),
                            MemoryModel.namespace_id == namespace,
                            MemoryModel.scope_key.in_(scopes),
                            MemoryModel.status == "active",
                            MemoryModel.deleted_time.is_(None),
                            (
                                MemoryModel.expires_at.is_(None)
                                | (MemoryModel.expires_at > func.now())
                            ),
                            ~select(MemorySettingModel.namespace_id)
                            .where(
                                MemorySettingModel.namespace_id == namespace,
                                MemorySettingModel.read_enabled.is_(False),
                            )
                            .exists(),
                            self._enabled_memory_scope(),
                        )
                    )
                )
                .unique()
                .scalars()
                .all()
            )
            memories = {row.id: memory_to_domain(row) for row in rows}
            return RecallSnapshot(enabled, memories)

    async def list(
        self, access: AccessContext, scope: str, limit: int, offset: int
    ) -> tuple[Memory, ...]:
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == access.namespace_id,
                        MemoryModel.scope_key == scope,
                        MemoryModel.scope_key.in_(access.readable_scopes),
                        MemoryModel.deleted_time.is_(None),
                        self._enabled_memory_scope(),
                    )
                    .order_by(MemoryModel.updated_at.desc(), MemoryModel.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return tuple(memory_to_domain(row) for row in rows)

    async def edit(
        self,
        access: AccessContext,
        memory_id: str,
        content: str,
        embedding: List[float],
        profile: str,
        expected_version: int,
    ) -> bool:
        async with self._factory() as session, session.begin():
            # 先无锁定位目标 scope，再按“scope → 目标记忆”的统一锁序操作，不再锁用户级设置行。
            target_scope = await session.scalar(
                select(MemoryModel.scope_key)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key.in_(access.writable_scopes),
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .limit(1)
            )
            if target_scope is None:
                return False
            now = datetime.now(UTC)
            scope_state = await lock_scope_state(
                session,
                access.namespace_id,
                target_scope,
            )
            row = await session.scalar(
                select(MemoryModel)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key == target_scope,
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.content = content
            row.search_document = func.to_tsvector(
                "athena_memory_chinese",
                content,
            )
            row.embedding = embedding
            row.embedding_profile_id = profile
            row.content_hash = _normalized_content_hash(content)
            row.superseded_by_id = None
            row.version = expected_version + 1
            row.updated_at = now
            await session.execute(
                delete(MemorySourceModel).where(MemorySourceModel.memory_id == memory_id)
            )
            scope_state.updated_at = now
            return True

    async def delete(self, access: AccessContext, memory_id: str, expected_version: int) -> bool:
        async with self._factory() as session, session.begin():
            # 先无锁定位目标 scope，再按“scope → 目标记忆”的统一锁序复核，不锁用户级设置行。
            target_scope = await session.scalar(
                select(MemoryModel.scope_key)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key.in_(access.writable_scopes),
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .limit(1)
            )
            if target_scope is None:
                return False
            now = datetime.now(UTC)
            scope_state = await lock_scope_state(
                session,
                access.namespace_id,
                target_scope,
            )
            row = await session.scalar(
                select(MemoryModel)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key == target_scope,
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return False
            await self._tombstone(session, row, now)
            scope_state.updated_at = now
            # 普通单条删除只墓碑目标；scope 级清理专用于关闭 scope，
            # 否则会把同一 scope 中仍然有效的其他记忆一并删除。
            return True
