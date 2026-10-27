"""PostgreSQL long-term memory implementation."""

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, and_, cast, delete as sql_delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import aliased

from domain.leases import LeaseGrant
from domain.memory import (
    MemoryListRequest,
    MemoryFact,
    MemoryFactIdentity,
    MemoryPage,
    MemoryRecord,
    MemoryRelationRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryWriteCommand,
    MemoryStateChangedError,
    MemorySuppressedError,
)
from domain.memory.suppression import (
    EPISODE_SUPPRESSION_FINGERPRINT,
    PROJECT_SUPPRESSION_FINGERPRINT,
    PROJECT_TURN_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)
from .lease_center import PostgresLeaseCenter
from domain.memory.repository_ports import MemoryRepositoryPort
from shared.contracts.memory import MemoryRecordMetadataSchema, memory_schema_to_domain

from ..models import (
    MemoryModel,
    MemoryRelationModel,
    MemorySourceSuppressionModel,
    MemoryEpisodeModel,
)


def _or_chinese_queries(terms: tuple[str, ...]):
    """把受限词项组合为可使用 GIN 的中文 OR tsquery。"""
    queries = [func.plainto_tsquery("chinese", term) for term in terms]
    combined = queries[0]
    for query in queries[1:]:
        combined = combined.op("||")(query)
    return combined


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata_json(metadata: dict[str, object]) -> str:
    """通过 JSON 边界校验新记忆元数据后生成稳定文本。"""
    raw_json = json.dumps(metadata, ensure_ascii=False)
    typed_metadata = MemoryRecordMetadataSchema.model_validate_json(raw_json)
    return typed_metadata.model_dump_json(exclude_none=True)


def _metadata_fact(raw_fact: object) -> MemoryFact | None:
    """在持久化边界校验 metadata 中的事实并转换为领域类型。"""
    if raw_fact is None:
        return None
    typed_fact = MemoryRecordMetadataSchema.model_validate({"fact": raw_fact}).fact
    if typed_fact is None:
        return None
    return memory_schema_to_domain(typed_fact)


def _fact_lock_id(fact: MemoryFact) -> int:
    """生成稳定的 PostgreSQL advisory lock 身份。"""
    return _identity_lock_id(
        (fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key)
    )


def _identity_lock_id(identity: tuple[str, str, str, str]) -> int:
    """对规范事实身份生成稳定的 PostgreSQL advisory lock。"""
    serialized = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return int.from_bytes(
        hashlib.sha256(serialized.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _suppression_fingerprint(metadata: dict[str, Any], content: str) -> str:
    """Create a content-free stable identity for a forgotten memory."""
    raw_fact = metadata.get("fact")
    fact = None
    if raw_fact is not None:
        try:
            fact = _metadata_fact(raw_fact)
        except (TypeError, ValueError):
            fact = None
    identity: tuple[Any, ...]
    if fact is not None:
        identity = (
            "fact",
            fact.scope_kind,
            fact.scope_id,
            fact.subject_key,
            fact.fact_key,
        )
    else:
        normalized_content = " ".join(content.casefold().split())
        identity = (
            "text",
            str(metadata.get("type", metadata.get("category", ""))),
            normalized_content,
        )
    serialized = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _source_turn_ids(metadata: dict[str, Any]) -> tuple[str, ...]:
    """Return source turn identities needed to prevent reconstitution."""
    source_ids: set[str] = set()
    source_turn_id = metadata.get("source_turn_id")
    if isinstance(source_turn_id, str) and source_turn_id:
        source_ids.add(source_turn_id)
    raw_fact = metadata.get("fact")
    if raw_fact is not None:
        try:
            fact = _metadata_fact(raw_fact)
        except (TypeError, ValueError):
            fact = None
        if fact is not None:
            source_ids.update(
                reference.source_id
                for reference in fact.source_refs
                if reference.source_type == "turn"
            )
    return tuple(sorted(source_ids))


def _suppression_lock_id(source_id: str, fingerprint: str) -> int:
    """Serialize write/delete operations for one source and forgotten item."""
    return source_suppression_lock_id(source_id, fingerprint)


async def _ensure_episode_source_not_suppressed(
    session: AsyncSession, source_ids: tuple[str, ...]
) -> None:
    """Serialize against forget operations that suppress a source episode."""
    for source_id in source_ids:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    _suppression_lock_id(
                        source_id, EPISODE_SUPPRESSION_FINGERPRINT
                    )
                )
            )
        )
        suppressed = await session.scalar(
            select(MemorySourceSuppressionModel.source_id)
            .where(
                MemorySourceSuppressionModel.source_type == "episode_turn",
                MemorySourceSuppressionModel.source_id == source_id,
                MemorySourceSuppressionModel.item_fingerprint
                == EPISODE_SUPPRESSION_FINGERPRINT,
            )
            .limit(1)
        )
        if suppressed is not None:
            raise MemorySuppressedError("memory_episode_source_forgotten")


async def _ensure_project_not_suppressed(
    session: AsyncSession, project_id: str | None
) -> None:
    """Block writes targeting a project space that has been forgotten."""
    if not project_id:
        return
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _suppression_lock_id(project_id, PROJECT_SUPPRESSION_FINGERPRINT)
            )
        )
    )
    suppressed = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "project",
            MemorySourceSuppressionModel.source_id == project_id,
            MemorySourceSuppressionModel.item_fingerprint
            == PROJECT_SUPPRESSION_FINGERPRINT,
        )
        .limit(1)
    )
    if suppressed is not None:
        raise MemorySuppressedError("memory_project_forgotten")


async def _ensure_project_turns_not_suppressed(
    session: AsyncSession, source_ids: tuple[str, ...]
) -> None:
    """Block old queued turns after their bound project space was forgotten."""
    await _lock_suppression_sources(
        session, source_ids, PROJECT_TURN_SUPPRESSION_FINGERPRINT
    )
    if not source_ids:
        return
    suppressed = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "project_turn",
            MemorySourceSuppressionModel.source_id.in_(source_ids),
            MemorySourceSuppressionModel.item_fingerprint
            == PROJECT_TURN_SUPPRESSION_FINGERPRINT,
        )
        .limit(1)
    )
    if suppressed is not None:
        raise MemorySuppressedError("memory_project_source_forgotten")


async def _lock_suppression_sources(
    session: AsyncSession,
    source_ids: tuple[str, ...],
    fingerprint: str,
) -> None:
    """Lock sources in stable order so delete and replay cannot race."""
    for source_id in source_ids:
        await session.execute(
            select(func.pg_advisory_xact_lock(_suppression_lock_id(source_id, fingerprint)))
        )


async def _ensure_not_suppressed(
    session: AsyncSession,
    source_ids: tuple[str, ...],
    fingerprint: str,
) -> None:
    """Reject a candidate when any of its original turns was forgotten."""
    if not source_ids:
        return
    suppressed = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "turn",
            MemorySourceSuppressionModel.source_id.in_(source_ids),
            MemorySourceSuppressionModel.item_fingerprint == fingerprint,
        )
        .limit(1)
    )
    if suppressed is not None:
        raise MemorySuppressedError("memory_source_forgotten")


def _row_fact_identity(row: MemoryModel) -> tuple[str, str, str, str] | None:
    """读取版本的事实锁身份；旧正文只按既有的 session 回退规则推导。"""
    metadata = _json(row.metadata_json, {})
    if not isinstance(metadata, dict):
        return None
    raw_fact = metadata.get("fact")
    if raw_fact is not None:
        try:
            fact = _metadata_fact(raw_fact)
        except (TypeError, ValueError):
            return None
        if fact is not None:
            return fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key
    key_match = _CONTENT_KEY.match(row.content.strip())
    session_id = metadata.get("session_id")
    if key_match is None or not isinstance(session_id, str) or not session_id:
        return None
    fact_key = key_match.group(1)
    category = row.category or row.memory_type or ""
    if category in {"preference", "profile"}:
        return "global", "global", "user:self", fact_key
    return "session", session_id, f"session:{session_id}", fact_key


_CONTENT_KEY = re.compile(r"^([a-z][a-z0-9_]{0,29}):\s*.+$", re.DOTALL)


def _validate_fact_content(content: str, fact: MemoryFact | None) -> None:
    """确保可检索正文与结构化事实键属于同一组。"""
    if fact is None:
        return
    match = _CONTENT_KEY.match(content.strip())
    if match is None or match.group(1) != fact.fact_key:
        raise ValueError("memory_fact_content_key_mismatch")


def _matching_fact_rows(
    rows: list[MemoryModel],
    fact: MemoryFact,
    *,
    session_id: str,
    legacy_content: str | None = None,
) -> list[MemoryModel]:
    """按解析器的范围、有效期与旧记录回退规则筛选事实成员。"""
    now = datetime.now(timezone.utc)
    identity = (fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key)
    matches: list[MemoryModel] = []
    for row in rows:
        if not _is_unexpired(row, now=now):
            continue
        metadata = _json(row.metadata_json, {})
        if not isinstance(metadata, dict):
            continue
        raw_fact = metadata.get("fact")
        stored_fact = None
        if raw_fact is not None:
            try:
                stored_fact = _metadata_fact(raw_fact)
            except (TypeError, ValueError):
                stored_fact = None
        if stored_fact is not None:
            stored_identity = (
                stored_fact.scope_kind,
                stored_fact.scope_id,
                stored_fact.subject_key,
                stored_fact.fact_key,
            )
            if stored_identity != identity:
                continue
            if stored_fact.valid_from is not None:
                try:
                    valid_from = datetime.fromisoformat(stored_fact.valid_from)
                except ValueError:
                    continue
                if valid_from.tzinfo is None:
                    valid_from = valid_from.replace(tzinfo=timezone.utc)
                if valid_from > now:
                    continue
            if (
                fact.claim_intent == "retract"
                and stored_fact.fact_value != fact.retracts_value
            ):
                continue
            matches.append(row)
            continue
        if legacy_content is None or metadata.get("session_id") != session_id:
            continue
        stored_content = row.content.casefold()
        if stored_content == legacy_content.casefold() or (
            fact.claim_intent == "retract"
            and stored_content
            == f"{fact.fact_key}: {fact.retracts_value}".casefold()
        ):
            matches.append(row)
    return sorted(matches, key=lambda row: row.id)


def _matching_fact_identity_rows(
    rows: list[MemoryModel], identity: MemoryFactIdentity
) -> list[MemoryModel]:
    """加载同一身份下当前有效的结构化事实，不回退到旧文本推断。"""
    now = datetime.now(timezone.utc)
    expected = (
        identity.scope_kind,
        identity.scope_id,
        identity.subject_key,
        identity.fact_key,
    )
    matches: list[MemoryModel] = []
    for row in rows:
        if not _is_unexpired(row, now=now):
            continue
        metadata = _json(row.metadata_json, {})
        if not isinstance(metadata, dict) or metadata.get("fact") is None:
            continue
        try:
            fact = _metadata_fact(metadata["fact"])
        except (TypeError, ValueError):
            continue
        if fact is None or (
            fact.scope_kind,
            fact.scope_id,
            fact.subject_key,
            fact.fact_key,
        ) != expected:
            continue
        if fact.valid_from is not None:
            try:
                valid_from = datetime.fromisoformat(fact.valid_from)
            except ValueError:
                continue
            if valid_from.tzinfo is None:
                valid_from = valid_from.replace(tzinfo=timezone.utc)
            if valid_from > now:
                continue
        matches.append(row)
    return sorted(matches, key=lambda row: row.id)


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed


def _is_unexpired(row: MemoryModel, *, now: datetime) -> bool:
    """使用与应用解析一致的规则排除过期记录。"""
    for value in (row.expires_at, row.valid_until):
        if value is None:
            continue
        try:
            deadline = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return False
    return True


class PostgresMemoryRepository(MemoryRepositoryPort):
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def initialize(self) -> None:
        return None

    async def search(self, request: MemorySearchRequest):
        async with self._session_factory() as session:
            filters = request.filters or {}
            terms = request.terms or (request.query,)
            text_query = _or_chinese_queries(terms)
            text_rank = func.ts_rank_cd(MemoryModel.content_fts, text_query)
            base_statement = select(
                MemoryModel, text_rank.label("search_rank")
            ).where(
                MemoryModel.status == MemoryStatus.ACTIVE.value,
                MemoryModel.validity_status != "invalid",
                MemoryModel.deleted_time.is_(None),
                *self._unexpired_conditions(),
            )
            if filters.get("session_id"):
                base_statement = base_statement.where(
                    MemoryModel.session_id == filters["session_id"]
                )
            scope_filter = filters.get("memory_scope")
            if isinstance(scope_filter, dict) and scope_filter.get("session_id"):
                metadata = cast(MemoryModel.metadata_json, JSONB)
                fact = metadata["fact"]
                scope_kind = fact["scope_kind"].astext
                scope_id = fact["scope_id"].astext
                metadata_scope_kind = metadata["scope_kind"].astext
                metadata_scope_id = metadata["scope_id"].astext
                session_id = str(scope_filter["session_id"])
                non_fact_scopes = [
                    MemoryModel.session_id == session_id,
                    MemoryModel.category.in_(("preference", "profile")),
                    and_(metadata_scope_kind == "global", metadata_scope_id == "global"),
                    and_(metadata_scope_kind == "session", metadata_scope_id == session_id),
                ]
                project_id = scope_filter.get("project_id")
                if isinstance(project_id, str) and project_id:
                    non_fact_scopes.append(
                        and_(metadata_scope_kind == "project", metadata_scope_id == project_id)
                    )
                allowed_scopes = [
                    and_(scope_kind == "global", scope_id == "global"),
                    and_(scope_kind == "session", scope_id == session_id),
                    and_(
                        fact.astext.is_(None),
                        or_(*non_fact_scopes),
                    ),
                ]
                if isinstance(project_id, str) and project_id:
                    allowed_scopes.append(
                        and_(scope_kind == "project", scope_id == project_id)
                    )
                base_statement = base_statement.where(or_(*allowed_scopes))
            rows = (
                await session.execute(
                    base_statement.where(
                        MemoryModel.content_fts.op("@@")(text_query)
                    ).order_by(
                        text_rank.desc(), MemoryModel.created_at.desc()
                    ).limit(request.limit)
                )
            ).all()
            if not rows:
                rows = (
                    await session.execute(
                        base_statement.where(
                            MemoryModel.content.ilike(f"%{request.query}%")
                        )
                        .order_by(MemoryModel.created_at.desc())
                        .limit(request.limit)
                    )
                ).all()
        values = [
            self._domain(row, score=float(score or 0.0)) for row, score in rows
        ]
        if request.record_access:
            await self.record_access([value.id for value in values])
        return values

    async def list_scope_index(
        self,
        *,
        session_id: str,
        project_id: str | None,
        limit: int = 80,
    ) -> list[MemoryRecord]:
        """读取构建当前作用域主题目录所需的有限有效记忆。

        参数：session_id/project_id 是显式绑定范围；limit 限制最多读取的目录来源数。
        返回：按最近更新排序的有效记忆，不增加访问计数。
        异常：数据库或记忆元数据读取失败时向上传播。
        """
        bounded_limit = min(120, max(1, limit))
        metadata = cast(MemoryModel.metadata_json, JSONB)
        fact = metadata["fact"]
        scope_kind = fact["scope_kind"].astext
        scope_id = fact["scope_id"].astext
        metadata_scope_kind = metadata["scope_kind"].astext
        metadata_scope_id = metadata["scope_id"].astext
        non_fact_scopes = [
            MemoryModel.session_id == session_id,
            MemoryModel.category.in_(("preference", "profile")),
            and_(metadata_scope_kind == "global", metadata_scope_id == "global"),
            and_(metadata_scope_kind == "session", metadata_scope_id == session_id),
        ]
        if project_id:
            non_fact_scopes.append(
                and_(metadata_scope_kind == "project", metadata_scope_id == project_id)
            )
        allowed_scopes = [
            and_(scope_kind == "global", scope_id == "global"),
            and_(scope_kind == "session", scope_id == session_id),
            and_(
                fact.astext.is_(None),
                or_(*non_fact_scopes),
            ),
        ]
        if project_id:
            allowed_scopes.append(
                and_(scope_kind == "project", scope_id == project_id)
            )
        statement = (
            select(MemoryModel)
            .where(
                MemoryModel.status == MemoryStatus.ACTIVE.value,
                MemoryModel.validity_status == "valid",
                MemoryModel.deleted_time.is_(None),
                *self._unexpired_conditions(),
                or_(*allowed_scopes),
            )
            .order_by(MemoryModel.created_at.desc(), MemoryModel.id)
            .limit(bounded_limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [self._domain(row) for row in rows]

    async def list_memories(self, request: MemoryListRequest):
        async with self._session_factory() as session:
            conditions = [MemoryModel.status != MemoryStatus.DELETED.value]
            if request.session_id:
                conditions.append(MemoryModel.session_id == request.session_id)
            base_conditions = list(conditions)
            expired_condition = or_(
                cast(MemoryModel.expires_at, DateTime(timezone=True)) <= func.now(),
                cast(MemoryModel.valid_until, DateTime(timezone=True)) <= func.now(),
            )
            if request.expired_only:
                conditions.append(expired_condition)
            rows = (
                (
                    await session.execute(
                        select(MemoryModel)
                        .where(*conditions)
                        .order_by(MemoryModel.created_at.desc())
                        .offset(request.offset)
                        .limit(request.limit)
                    )
                )
                .scalars()
                .all()
            )
            total = (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryModel)
                    .where(*conditions)
                )
                or 0
            )
            expired = await session.scalar(select(func.count()).select_from(MemoryModel).where(*base_conditions, expired_condition)) or 0
            recent_week = await session.scalar(select(func.count()).select_from(MemoryModel).where(
                *base_conditions, cast(MemoryModel.created_at, DateTime(timezone=True)) >= datetime.now(timezone.utc) - timedelta(days=7)
            )) or 0
        values = tuple(self._domain(row) for row in rows)
        return MemoryPage(
            values,
            int(total),
            {"total": int(total), "expired": int(expired), "recent_week": int(recent_week)},
        )

    async def list_fact_group(
        self, fact: MemoryFact, *, session_id: str
    ) -> list[MemoryRecord]:
        """为冲突感知读取加载同范围、同主体和同属性的有效事实。"""
        key_prefix = f"{fact.fact_key}:"
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(MemoryModel).where(
                        func.left(MemoryModel.content, len(key_prefix)) == key_prefix,
                        MemoryModel.status == MemoryStatus.ACTIVE.value,
                        MemoryModel.validity_status == "valid",
                        MemoryModel.deleted_time.is_(None),
                    )
                )
            ).scalars().all()
        matches = _matching_fact_rows(rows, fact, session_id=session_id)
        return [self._domain(row) for row in matches]

    async def list_fact_group_by_identity(
        self, identity: MemoryFactIdentity
    ) -> list[MemoryRecord]:
        """返回身份匹配且仍可用于上下文的结构化事实组。"""
        key_prefix = f"{identity.fact_key}:"
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(MemoryModel).where(
                        func.left(MemoryModel.content, len(key_prefix)) == key_prefix,
                        MemoryModel.status == MemoryStatus.ACTIVE.value,
                        MemoryModel.validity_status == "valid",
                        MemoryModel.deleted_time.is_(None),
                        *self._unexpired_conditions(),
                    )
                )
            ).scalars().all()
        matches = _matching_fact_identity_rows(rows, identity)
        return [self._domain(row) for row in matches]

    async def get(self, memory_id: str):
        async with self._session_factory() as session:
            row = await session.get(MemoryModel, memory_id)
        return None if row is None else self._domain(row)

    async def revisions(self, memory_id: str):
        async with self._session_factory() as session:
            row = await session.get(MemoryModel, memory_id)
            if row is None:
                return []
            logical_id = getattr(row, "logical_memory_id", None) or row.id
            rows = (
                (
                    await session.execute(
                        select(MemoryModel)
                        .where(MemoryModel.logical_memory_id == logical_id)
                        .order_by(MemoryModel.revision)
                    )
                )
                .scalars()
                .all()
            )
        return [self._domain(item) for item in rows] or [self._domain(row)]

    async def related_entries(self, memory_id: str) -> list[MemoryRelationRecord]:
        """Load direct replacement/conflict relations with the related row."""
        source = aliased(MemoryModel)
        target = aliased(MemoryModel)
        statement = (
            select(MemoryRelationModel, source, target)
            .join(source, source.id == MemoryRelationModel.source_memory_id)
            .join(target, target.id == MemoryRelationModel.target_memory_id)
            .where(
                or_(
                    MemoryRelationModel.source_memory_id == memory_id,
                    MemoryRelationModel.target_memory_id == memory_id,
                )
            )
            .order_by(MemoryRelationModel.created_at, MemoryRelationModel.id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        related: list[MemoryRelationRecord] = []
        for relation, source_row, target_row in rows:
            other = (
                target_row
                if relation.source_memory_id == memory_id
                else source_row
            )
            related.append(
                MemoryRelationRecord(
                    id=relation.id,
                    kind=relation.kind,
                    related_memory_id=other.id,
                    related_content=other.content,
                    related_status=other.status,
                    created_at=relation.created_at,
                )
            )
        return related

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        """创建人工记忆；后台来源必须走带租约的独立入口。"""
        return await self._create_memory(command, lease=None)

    async def create_memory_from_worker(
        self, command: MemoryWriteCommand, *, lease: LeaseGrant
    ) -> str:
        """在后台记忆 grant 下创建或幂等恢复事实记忆。"""
        source_turn_id = str(command.metadata.get("source_turn_id") or "")
        if (
            lease.key.resource_type != "memory_processing"
            or lease.key.resource_id != source_turn_id
        ):
            raise ValueError("memory_processing_lease_mismatch")
        return await self._create_memory(command, lease=lease)

    async def _create_memory(
        self, command: MemoryWriteCommand, *, lease: LeaseGrant | None
    ) -> str:
        """Create once per operation key; duplicate enqueues stay idempotent."""
        key = (
            command.operation_key
            or f"memory-{datetime.now(timezone.utc).timestamp()}"
        )
        metadata_json = _metadata_json(command.metadata)
        typed_metadata = memory_schema_to_domain(
            MemoryRecordMetadataSchema.model_validate_json(metadata_json)
        )
        _validate_fact_content(command.content, typed_metadata.fact)
        source_ids = _source_turn_ids(command.metadata)
        suppression_fingerprint = _suppression_fingerprint(
            command.metadata, command.content
        )
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(MemoryModel, key)
                if existing is not None:
                    if existing.status == MemoryStatus.DELETED.value:
                        raise MemorySuppressedError("memory_source_forgotten")
                    return key
                if typed_metadata.fact is not None:
                    await session.execute(
                        select(func.pg_advisory_xact_lock(_fact_lock_id(typed_metadata.fact)))
                    )
                    existing = await session.get(MemoryModel, key)
                    if existing is not None:
                        if existing.status == MemoryStatus.DELETED.value:
                            raise MemorySuppressedError("memory_source_forgotten")
                        return key
                project_id = (
                    typed_metadata.fact.scope_id
                    if typed_metadata.fact is not None
                    and typed_metadata.fact.scope_kind == "project"
                    else str(command.metadata.get("scope_id", ""))
                    if command.metadata.get("scope_kind") == "project"
                    else None
                )
                await _ensure_project_not_suppressed(session, project_id)
                await _ensure_project_turns_not_suppressed(session, source_ids)
                if command.metadata.get("source_kind") == "episode":
                    await _ensure_episode_source_not_suppressed(session, source_ids)
                await _lock_suppression_sources(
                    session, source_ids, suppression_fingerprint
                )
                await _ensure_not_suppressed(
                    session, source_ids, suppression_fingerprint
                )
                if typed_metadata.fact is not None:
                    if command.expected_memory_ids is not None:
                        current_ids = await self._active_fact_group_ids(
                            session,
                            typed_metadata.fact,
                            content=command.content,
                            session_id=str(command.metadata.get("session_id", "")),
                        )
                        if current_ids != tuple(sorted(command.expected_memory_ids)):
                            raise MemoryStateChangedError("memory_fact_group_changed")
                if lease is not None:
                    await self._leases.assert_current_in_transaction(
                        session, lease
                    )
                elif command.metadata.get("source") in {"extraction", "episode_reusable_learning"}:
                    raise ValueError("background_memory_write_requires_lease")
                await session.execute(
                    pg_insert(MemoryModel)
                    .values(
                        id=key,
                        logical_memory_id=key,
                        session_id=str(command.metadata.get("session_id", "")),
                        content=command.content,
                        metadata_json=metadata_json,
                        created_at=_now(),
                        memory_type=str(command.metadata.get("type", "")) or None,
                        category=str(command.metadata.get("category", "")) or None,
                        confidence=float(command.metadata.get("confidence", 0.0)),
                        source_kind="extraction",
                        status=MemoryStatus.ACTIVE.value,
                        source_turn_id=str(
                            command.metadata.get("source_turn_id", "")
                        )
                        or None,
                        last_observed_at=_now(),
                        validity_status="valid",
                        revision=1,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
        return key

    async def _active_fact_group_ids(
        self,
        session: AsyncSession,
        fact: MemoryFact,
        *,
        content: str,
        session_id: str,
    ) -> tuple[str, ...]:
        """在写入事务中读取解析器所见的当前事实组。"""
        key_prefix = f"{fact.fact_key}:"
        rows = (
            await session.execute(
                select(MemoryModel).where(
                    func.left(MemoryModel.content, len(key_prefix)) == key_prefix,
                    MemoryModel.status == MemoryStatus.ACTIVE.value,
                    MemoryModel.validity_status == "valid",
                    MemoryModel.deleted_time.is_(None),
                )
            )
        ).scalars().all()
        matches = _matching_fact_rows(
            rows,
            fact,
            session_id=session_id,
            legacy_content=content,
        )
        return tuple(row.id for row in matches)

    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata=None,
        operation_key=None,
        expected_memory_ids: tuple[str, ...] | None = None,
    ):
        """人工修订记忆，不接受可选的后台 grant。"""
        return await self._revise(
            memory_id,
            content,
            metadata=metadata,
            operation_key=operation_key,
            expected_memory_ids=expected_memory_ids,
            lease=None,
        )

    async def revise_from_worker(
        self,
        memory_id: str,
        content: str,
        *,
        metadata=None,
        operation_key=None,
        expected_memory_ids: tuple[str, ...] | None = None,
        lease: LeaseGrant,
    ):
        """在记忆处理 grant 下修订后台提取的事实。"""
        source_turn_id = str((metadata or {}).get("source_turn_id") or "")
        if (
            lease.key.resource_type != "memory_processing"
            or lease.key.resource_id != source_turn_id
        ):
            raise ValueError("memory_processing_lease_mismatch")
        return await self._revise(
            memory_id,
            content,
            metadata=metadata,
            operation_key=operation_key,
            expected_memory_ids=expected_memory_ids,
            lease=lease,
        )

    async def _revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata=None,
        operation_key=None,
        expected_memory_ids: tuple[str, ...] | None = None,
        lease=None,
    ):
        """Insert a new immutable revision and supersede the previous row."""
        metadata_payload = metadata or {}
        metadata_json = _metadata_json(metadata_payload)
        typed_metadata = memory_schema_to_domain(
            MemoryRecordMetadataSchema.model_validate_json(metadata_json)
        )
        _validate_fact_content(content, typed_metadata.fact)
        source_ids = _source_turn_ids(metadata_payload)
        suppression_fingerprint = _suppression_fingerprint(metadata_payload, content)
        async with self._session_factory() as session:
            async with session.begin():
                new_id = (
                    operation_key or ""
                )
                existing = await session.get(MemoryModel, new_id) if new_id else None
                if existing is not None:
                    if existing.status == MemoryStatus.DELETED.value:
                        raise MemorySuppressedError("memory_source_forgotten")
                    return new_id
                if typed_metadata.fact is not None:
                    await session.execute(
                        select(func.pg_advisory_xact_lock(_fact_lock_id(typed_metadata.fact)))
                    )
                    existing = await session.get(MemoryModel, new_id)
                    if existing is not None:
                        if existing.status == MemoryStatus.DELETED.value:
                            raise MemorySuppressedError("memory_source_forgotten")
                        return new_id
                project_id = (
                    typed_metadata.fact.scope_id
                    if typed_metadata.fact is not None
                    and typed_metadata.fact.scope_kind == "project"
                    else str(metadata_payload.get("scope_id", ""))
                    if metadata_payload.get("scope_kind") == "project"
                    else None
                )
                await _ensure_project_not_suppressed(session, project_id)
                await _ensure_project_turns_not_suppressed(session, source_ids)
                if metadata_payload.get("source_kind") == "episode":
                    await _ensure_episode_source_not_suppressed(session, source_ids)
                await _lock_suppression_sources(
                    session, source_ids, suppression_fingerprint
                )
                await _ensure_not_suppressed(
                    session, source_ids, suppression_fingerprint
                )
                if typed_metadata.fact is not None:
                    if expected_memory_ids is not None:
                        current_ids = await self._active_fact_group_ids(
                            session,
                            typed_metadata.fact,
                            content=content,
                            session_id=str(metadata_payload.get("session_id", "")),
                        )
                        if current_ids != tuple(sorted(expected_memory_ids)):
                            raise MemoryStateChangedError("memory_fact_group_changed")
                else:
                    old_hint = await session.get(MemoryModel, memory_id)
                    identity = (
                        None if old_hint is None else _row_fact_identity(old_hint)
                    )
                    if identity is not None:
                        await session.execute(
                            select(func.pg_advisory_xact_lock(_identity_lock_id(identity)))
                        )
                old = await session.scalar(
                    select(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .with_for_update()
                )
                if old is None:
                    return None
                if not new_id:
                    new_id = f"{old.logical_memory_id}:rev:{old.revision + 1}"
                existing_revision = await session.get(MemoryModel, new_id)
                if existing_revision is not None:
                    if existing_revision.status == MemoryStatus.DELETED.value:
                        raise MemorySuppressedError("memory_source_forgotten")
                    return new_id
                if old.status != MemoryStatus.ACTIVE.value:
                    return None
                if lease is not None:
                    await self._leases.assert_current_in_transaction(session, lease)
                elif metadata_payload.get("source") == "extraction":
                    raise ValueError("background_memory_write_requires_lease")
                old.status = MemoryStatus.SUPERSEDED.value
                session.add(
                    MemoryModel(
                        id=new_id,
                        logical_memory_id=old.logical_memory_id,
                        session_id=old.session_id,
                        content=content,
                        metadata_json=metadata_json,
                        created_at=_now(),
                        memory_type=old.memory_type,
                        category=old.category,
                        confidence=old.confidence,
                        source_kind=old.source_kind,
                        status=MemoryStatus.ACTIVE.value,
                        source_turn_id=str(
                            (metadata or {}).get("source_turn_id", "")
                        )
                        or None,
                        last_observed_at=_now(),
                        validity_status=(
                            "invalid"
                            if typed_metadata.fact is not None
                            and typed_metadata.fact.claim_intent == "retract"
                            else (
                                "valid"
                                if typed_metadata.fact is not None
                                else old.validity_status
                            )
                        ),
                        valid_until=(
                            typed_metadata.fact.valid_until
                            if typed_metadata.fact is not None
                            else old.valid_until
                        ),
                        expires_at=(
                            None if typed_metadata.fact is not None else old.expires_at
                        ),
                        revision=old.revision + 1,
                    )
                )
        return new_id

    async def add_relation(
        self,
        source_memory_id: str,
        target_memory_id: str,
        kind: str,
        *,
        operation_id: str,
    ) -> None:
        """人工维护关系；后台关系写入使用 grant 绑定入口。"""
        await self._add_relation(
            source_memory_id,
            target_memory_id,
            kind,
            operation_id=operation_id,
            lease=None,
        )

    async def add_relation_from_worker(
        self,
        source_memory_id: str,
        target_memory_id: str,
        kind: str,
        *,
        operation_id: str,
        lease: LeaseGrant,
    ) -> None:
        """在源记忆轮次的当前 grant 下保存后台关系。"""
        if lease.key.resource_type != "memory_processing":
            raise ValueError("memory_processing_lease_required")
        await self._add_relation(
            source_memory_id,
            target_memory_id,
            kind,
            operation_id=operation_id,
            lease=lease,
        )

    async def _add_relation(
        self,
        source_memory_id: str,
        target_memory_id: str,
        kind: str,
        *,
        operation_id: str,
        lease=None,
    ) -> None:
        """幂等保存替代或矛盾关系，并校验两端记忆存在。"""
        if kind not in {"supersedes", "contradicts"}:
            raise ValueError("memory_relation_kind_invalid")
        if source_memory_id == target_memory_id:
            raise ValueError("memory_relation_self_reference")
        worker_source_memory_id = source_memory_id
        if kind == "contradicts":
            source_memory_id, target_memory_id = sorted(
                (source_memory_id, target_memory_id)
            )
        relation_id = hashlib.sha256(
            f"{operation_id}:{kind}:{source_memory_id}:{target_memory_id}".encode()
        ).hexdigest()
        async with self._session_factory() as session:
            async with session.begin():
                if lease is not None:
                    locked = (
                        await session.scalars(
                        select(MemoryModel)
                        .where(MemoryModel.id.in_(
                            (source_memory_id, target_memory_id)
                        ))
                        .order_by(MemoryModel.id)
                        .with_for_update()
                        )
                    ).all()
                    source = next(
                        (row for row in locked if row.id == worker_source_memory_id), None
                    )
                    if source is None or source.source_turn_id != lease.key.resource_id:
                        raise ValueError("memory_relation_source_turn_mismatch")
                    await self._leases.assert_current_in_transaction(session, lease)
                elif operation_id.startswith("memop_"):
                    raise ValueError("background_memory_relation_requires_lease")
                await session.execute(
                    pg_insert(MemoryRelationModel)
                    .values(
                        id=relation_id,
                        source_memory_id=source_memory_id,
                        target_memory_id=target_memory_id,
                        kind=kind,
                        operation_id=operation_id,
                        created_at=_now(),
                    )
                    .on_conflict_do_nothing()
                )

    async def update_validity(
        self, memory_id: str, validity_status: str, *, valid_until=None
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._locked_memory_row(session, memory_id)
                if row is None:
                    return False
                row.validity_status = validity_status
                row.valid_until = valid_until
        return True

    async def delete(self, memory_id: str) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._locked_memory_row(session, memory_id)
                if row is None:
                    return False
                metadata = _json(row.metadata_json, {})
                if not isinstance(metadata, dict):
                    metadata = {}
                source_ids = _source_turn_ids(metadata)
                fingerprint = _suppression_fingerprint(metadata, row.content)
                await _lock_suppression_sources(
                    session, source_ids, EPISODE_SUPPRESSION_FINGERPRINT
                )
                await _lock_suppression_sources(session, source_ids, fingerprint)
                episode_ids = list(
                    (
                        await session.scalars(
                            select(MemoryEpisodeModel.episode_id).where(
                                MemoryEpisodeModel.turn_id.in_(source_ids)
                            )
                        )
                    ).all()
                ) if source_ids else []
                if episode_ids:
                    episode_id_field = cast(MemoryModel.metadata_json, JSONB)[
                        "episode_id"
                    ].astext
                    derived_rows = (
                        await session.scalars(
                            select(MemoryModel)
                            .where(
                                episode_id_field.in_(episode_ids),
                                MemoryModel.status == MemoryStatus.ACTIVE.value,
                                MemoryModel.deleted_time.is_(None),
                            )
                            .with_for_update()
                        )
                    ).all()
                    for derived in derived_rows:
                        derived_metadata = _json(derived.metadata_json, {})
                        if not isinstance(derived_metadata, dict):
                            derived_metadata = {}
                        derived_sources = _source_turn_ids(derived_metadata)
                        derived_fingerprint = _suppression_fingerprint(
                            derived_metadata, derived.content
                        )
                        await _lock_suppression_sources(
                            session, derived_sources, derived_fingerprint
                        )
                        for derived_source in derived_sources:
                            await session.execute(
                                pg_insert(MemorySourceSuppressionModel)
                                .values(
                                    source_type="turn",
                                    source_id=derived_source,
                                    item_fingerprint=derived_fingerprint,
                                    created_at=_now(),
                                )
                                .on_conflict_do_nothing()
                            )
                        derived.status = MemoryStatus.DELETED.value
                        derived.deleted_time = _now()
                for source_id in source_ids:
                    await session.execute(
                        pg_insert(MemorySourceSuppressionModel)
                        .values(
                            source_type="turn",
                            source_id=source_id,
                            item_fingerprint=fingerprint,
                            created_at=_now(),
                        )
                        .on_conflict_do_nothing()
                    )
                    await session.execute(
                        pg_insert(MemorySourceSuppressionModel)
                        .values(
                            source_type="episode_turn",
                            source_id=source_id,
                            item_fingerprint=EPISODE_SUPPRESSION_FINGERPRINT,
                            created_at=_now(),
                        )
                        .on_conflict_do_nothing()
                    )
                if source_ids:
                    # A task summary is a derived view of its source turn. Remove it
                    # in the same transaction; the suppression row prevents retries
                    # from recreating it from the forgotten turn.
                    await session.execute(
                        sql_delete(MemoryEpisodeModel).where(
                            MemoryEpisodeModel.turn_id.in_(source_ids)
                        )
                    )
                row.status = MemoryStatus.DELETED.value
                row.deleted_time = _now()
        return True

    async def _locked_memory_row(
        self, session: AsyncSession, memory_id: str
    ) -> MemoryModel | None:
        """按事实组锁序列化管理变更，再锁定并刷新目标版本。"""
        hint = await session.get(MemoryModel, memory_id)
        if hint is None:
            return None
        identity = _row_fact_identity(hint)
        if identity is not None:
            await session.execute(
                select(func.pg_advisory_xact_lock(_identity_lock_id(identity)))
            )
        return await session.scalar(
            select(MemoryModel)
            .where(MemoryModel.id == memory_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def record_access(self, memory_ids: list[str]) -> int:
        """原子记录召回访问；参数 memory_ids 为本次选中的 ID 列表，重复 ID 只计一次。

        返回更新的记录数；数据库异常向上传播，不主动抛出业务异常。
        """
        if not memory_ids:
            return 0
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(set(memory_ids)), MemoryModel.status == "active", MemoryModel.validity_status != "invalid", MemoryModel.deleted_time.is_(None), *self._unexpired_conditions())
                    .values(access_count=func.coalesce(MemoryModel.access_count, 0) + 1, last_accessed_at=_now())
                )
        return int(result.rowcount)

    @staticmethod
    def _unexpired_conditions() -> list[ColumnElement[bool]]:
        """生成有效期限条件；无参数，返回 SQL 条件列表，不主动抛出业务异常。"""
        return [
            or_(column.is_(None), cast(column, DateTime(timezone=True)) > func.now())
            for column in (MemoryModel.expires_at, MemoryModel.valid_until)
        ]

    async def flush_access_stats(self) -> int:
        """统计已随访问提交；无参数，返回待刷新的数量 0，无业务异常。"""
        return 0

    @staticmethod
    def _domain(row, *, score=None):
        return MemoryRecord(
            row.id,
            row.content,
            _json(row.metadata_json, {}),
            score,
            row.source_kind,
            MemoryStatus(row.status),
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_accessed_at=row.last_accessed_at,
            access_count=row.access_count,
            last_observed_at=row.last_observed_at,
            valid_until=row.valid_until,
            validity_status=row.validity_status,
        )

__all__ = ["PostgresMemoryRepository"]
