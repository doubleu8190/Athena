"""PostgreSQL 记忆仓储与混合候选查询。"""

from __future__ import annotations

import hashlib
import json
from builtins import list as List
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Text, delete, func, select, update
from sqlalchemy.dialects.postgresql import TSQUERY, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from adapter.memory.model import MemoryModel, MemorySettingModel, MemorySourceModel
from domain.memory.model import (
    AccessContext,
    AddMemory,
    Memory,
    MemorySource,
    RecallSnapshot,
)


def _request_hash(request: AddMemory) -> str:
    """计算新增请求摘要，不把记忆正文复制到幂等存储。"""
    canonical = json.dumps(
        {
            "scope": request.scope_key,
            "content": request.content,
            "status": request.status,
            "origin": request.origin,
            "sources": [(source.kind, source.reference) for source in request.sources],
            "expires_at": request.expires_at.isoformat() if request.expires_at else None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class MemoryRepository:
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
            statement = (
                insert(MemorySettingModel)
                .values(
                    namespace_id=namespace_id,
                    read_enabled=read_enabled,
                    auto_extract_enabled=auto_extract_enabled,
                )
                .on_conflict_do_update(
                    index_elements=["namespace_id"],
                    set_={
                        "read_enabled": read_enabled,
                        "auto_extract_enabled": auto_extract_enabled,
                    },
                )
            )
            await session.execute(statement)

    async def get_by_request(self, request: AddMemory) -> Memory | None:
        async with self._factory() as session:
            row = await session.scalar(
                select(MemoryModel).where(
                    MemoryModel.namespace_id == request.access.namespace_id,
                    MemoryModel.request_id == request.request_id,
                )
            )
            if row is None:
                return None
            if row.request_hash != _request_hash(request):
                raise ValueError("memory_idempotency_conflict")
            if row.deleted_time is not None:
                raise ValueError("memory_request_deleted")
            return self._to_domain(row)

    async def add(self, request: AddMemory, embedding: List[float], profile: str) -> Memory:
        request_hash = _request_hash(request)
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            if request.origin == "extracted":
                await session.execute(
                    insert(MemorySettingModel)
                    .values(namespace_id=request.access.namespace_id)
                    .on_conflict_do_nothing(index_elements=["namespace_id"])
                )
                auto_extract_enabled = await session.scalar(
                    select(MemorySettingModel.auto_extract_enabled)
                    .where(MemorySettingModel.namespace_id == request.access.namespace_id)
                    .with_for_update(read=True)
                )
                if auto_extract_enabled is False:
                    raise ValueError("memory_auto_extract_disabled")
            statement = (
                insert(MemoryModel)
                .values(
                    id=f"mem_{uuid4().hex}",
                    namespace_id=request.access.namespace_id,
                    scope_key=request.scope_key,
                    content=request.content,
                    search_document=func.to_tsvector("athena_memory_chinese", request.content),
                    embedding=embedding,
                    embedding_profile_id=profile,
                    request_id=request.request_id,
                    request_hash=request_hash,
                    status=request.status,
                    origin=request.origin,
                    version=1,
                    expires_at=request.expires_at,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_memories_request")
                .returning(MemoryModel.id)
            )
            memory_id = await session.scalar(statement)
            if memory_id is None:
                existing = await session.scalar(
                    select(MemoryModel)
                    .where(
                        MemoryModel.namespace_id == request.access.namespace_id,
                        MemoryModel.request_id == request.request_id,
                    )
                    .with_for_update()
                )
                if existing is None or existing.request_hash != request_hash:
                    raise ValueError("memory_idempotency_conflict")
                if existing.deleted_time is not None:
                    raise ValueError("memory_request_deleted")
                return self._to_domain(existing)
            for source in request.sources:
                session.add(
                    MemorySourceModel(
                        id=f"msrc_{uuid4().hex}",
                        memory_id=memory_id,
                        source_kind=source.kind,
                        source_ref=source.reference,
                    )
                )
            row = await session.scalar(select(MemoryModel).where(MemoryModel.id == memory_id))
            assert row is not None
            return Memory(
                row.id,
                row.namespace_id,
                row.scope_key,
                row.content,
                row.status,
                row.origin,
                row.version,
                row.expires_at,
                request.sources,
                row.created_at,
                row.updated_at,
            )

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
                    )
                    .order_by(rank.desc(), MemoryModel.id)
                    .limit(limit)
                )
            ).all()
            return [(self._to_domain(row[0]), float(row[1])) for row in rows]

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
                    )
                    .order_by(distance, MemoryModel.id)
                    .limit(limit)
                )
            ).all()
            return [(self._to_domain(row[0]), 1.0 - float(row[1])) for row in rows]

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
                        )
                    )
                )
                .unique()
                .scalars()
                .all()
            )
            memories = {row.id: self._to_domain(row) for row in rows}
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
                    )
                    .order_by(MemoryModel.updated_at.desc(), MemoryModel.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return tuple(self._to_domain(row) for row in rows)

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
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key.in_(access.writable_scopes),
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .values(
                    content=content,
                    search_document=func.to_tsvector("athena_memory_chinese", content),
                    embedding=embedding,
                    embedding_profile_id=profile,
                    version=expected_version + 1,
                    updated_at=datetime.now(UTC),
                )
                .returning(MemoryModel.id)
            )
            updated_id = result.scalar_one_or_none()
            if updated_id is None:
                return False
            await session.execute(
                delete(MemorySourceModel).where(MemorySourceModel.memory_id == memory_id)
            )
            return True

    async def confirm(self, access: AccessContext, memory_id: str, expected_version: int) -> bool:
        async with self._factory() as session, session.begin():
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key.in_(access.writable_scopes),
                    MemoryModel.version == expected_version,
                    MemoryModel.status == "pending",
                    MemoryModel.deleted_time.is_(None),
                    MemoryModel.embedding.is_not(None),
                )
                .values(status="active", version=expected_version + 1, updated_at=datetime.now(UTC))
                .returning(MemoryModel.id)
            )
            return result.scalar_one_or_none() is not None

    async def delete(self, access: AccessContext, memory_id: str, expected_version: int) -> bool:
        async with self._factory() as session, session.begin():
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.id == memory_id,
                    MemoryModel.namespace_id == access.namespace_id,
                    MemoryModel.scope_key.in_(access.writable_scopes),
                    MemoryModel.version == expected_version,
                    MemoryModel.deleted_time.is_(None),
                )
                .values(
                    content="",
                    search_document=func.to_tsvector("athena_memory_chinese", ""),
                    embedding=None,
                    version=expected_version + 1,
                    deleted_time=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                .returning(MemoryModel.id)
            )
            deleted_id = result.scalar_one_or_none()
            if deleted_id is None:
                return False
            await session.execute(
                delete(MemorySourceModel).where(MemorySourceModel.memory_id == memory_id)
            )
            return True

    @staticmethod
    def _to_domain(row: MemoryModel) -> Memory:
        sources = tuple(MemorySource(item.source_kind, item.source_ref) for item in row.sources)
        return Memory(
            row.id,
            row.namespace_id,
            row.scope_key,
            row.content,
            row.status,
            row.origin,
            row.version,
            row.expires_at,
            sources,
            row.created_at,
            row.updated_at,
        )
