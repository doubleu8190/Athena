"""PostgreSQL long-term memory implementation."""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    and_,
    cast,
    delete as sql_delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import aliased

from backend.src.shared.logging import get_logger
from domain.leases import LeaseGrant, LeaseKey
from domain.memory import (
    MemoryResolution,
    ResolutionAction,
    MemoryRecordMetadata,
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
from application.contracts.memory import (
    MemoryRecordMetadataSchema,
    memory_schema_to_domain,
    memory_domain_to_schema,
)

from ..models import (
    MemoryModel,
    MemoryRelationModel,
    MemorySourceSuppressionModel,
    MemoryEpisodeModel,
)

logger = get_logger(__name__)


def _fact_identity_conditions(
    fact: MemoryFact | MemoryFactIdentity,
) -> list[ColumnElement[bool]]:
    """按 JSON 中的规范身份限定范围、主体和事实键。"""
    stored = cast(MemoryModel.metadata_json, JSONB)["fact"]
    return [
        stored[name].astext == value
        for name, value in (
            ("scope_kind", fact.scope_kind),
            ("scope_id", fact.scope_id),
            ("subject_key", fact.subject_key),
            ("fact_key", fact.fact_key),
        )
    ]


def _visible_scope_condition(
    session_id: str, project_id: str | None
) -> ColumnElement[bool]:
    """构造当前会话可访问的事实及非事实记忆范围。"""
    metadata = cast(MemoryModel.metadata_json, JSONB)
    fact = metadata["fact"]
    non_fact_scopes = [
        MemoryModel.session_id == session_id,
        MemoryModel.category.in_(("preference", "profile")),
        and_(metadata["scope_kind"].astext == "global", metadata["scope_id"].astext == "global"),
        and_(metadata["scope_kind"].astext == "session", metadata["scope_id"].astext == session_id),
    ]
    scopes = [
        and_(fact["scope_kind"].astext == "global", fact["scope_id"].astext == "global"),
        and_(fact["scope_kind"].astext == "session", fact["scope_id"].astext == session_id),
    ]
    if project_id:
        non_fact_scopes.append(
            and_(metadata["scope_kind"].astext == "project", metadata["scope_id"].astext == project_id)
        )
        scopes.append(and_(fact["scope_kind"].astext == "project", fact["scope_id"].astext == project_id))
    scopes.append(and_(fact.astext.is_(None), or_(*non_fact_scopes)))
    return or_(*scopes)


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
    return typed_metadata.model_dump_json(
        exclude_none=True,
        exclude={
            "session_id",
            "source_turn_id",
            "type",
            "category",
            "confidence",
            "source_kind",
        },
    )


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
    fact = _metadata_fact(raw_fact)
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
            select(
                func.pg_advisory_xact_lock(_suppression_lock_id(source_id, fingerprint))
            )
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
    """读取版本的结构化事实锁身份；非事实记忆不属于事实组。"""
    metadata = _json(row.metadata_json)
    if not isinstance(metadata, dict):
        return None
    raw_fact = metadata.get("fact")
    if raw_fact is not None:
        fact = _metadata_fact(raw_fact)
        if fact is not None:
            return fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key
    return None


_CONTENT_KEY = re.compile(r"^([a-z][a-z0-9_]{0,29}):\s*.+$", re.DOTALL)


def _validate_fact_content(content: str, fact: MemoryFact | None) -> None:
    """确保可检索正文与结构化事实键属于同一组。"""
    if fact is None:
        return
    match = _CONTENT_KEY.match(content.strip())
    if match is None or match.group(1) != fact.fact_key:
        raise ValueError("memory_fact_content_key_mismatch")


def _matching_fact_rows(
    rows: Sequence[MemoryModel],
    identity: MemoryFact | MemoryFactIdentity,
) -> list[MemoryModel]:
    """按身份和有效期筛选事实；撤回候选还须匹配目标值。"""
    now = datetime.now(timezone.utc)
    expected = (identity.scope_kind, identity.scope_id, identity.subject_key, identity.fact_key)
    matches: list[MemoryModel] = []
    for row in rows:
        if not _is_unexpired(row, now=now):
            logger.info(
                "记忆已过期，跳过",
                extra={"row_id": row.id, "expires_at": row.expires_at},
            )
            continue
        metadata = _json(row.metadata_json)
        raw_fact = metadata.get("fact")
        stored_fact = _metadata_fact(raw_fact)
        if stored_fact is not None:
            stored_identity = (
                stored_fact.scope_kind,
                stored_fact.scope_id,
                stored_fact.subject_key,
                stored_fact.fact_key,
            )
            if stored_identity != expected:
                continue
            if stored_fact.valid_from is not None:
                try:
                    valid_from = datetime.fromisoformat(stored_fact.valid_from)
                except ValueError:
                    logger.warning(
                        "Invalid valid_from format.", extra={"row_id": row.id}
                    )
                    continue
                if valid_from.tzinfo is None:
                    valid_from = valid_from.replace(tzinfo=timezone.utc)
                if valid_from > now:
                    continue
            if (
                isinstance(identity, MemoryFact)
                and identity.claim_intent == "retract"
                and stored_fact.fact_value != identity.retracts_value
            ):
                logger.info(
                    "记忆指跟要求撤回的目标值不匹配，跳过",
                    extra={
                        "row_id": row.id,
                        "stored_value": stored_fact.fact_value,
                        "retracts_value": identity.retracts_value,
                    },
                )
                continue
            matches.append(row)
    return sorted(matches, key=lambda row: row.id)


def _json(value: str) -> dict[str, Any]:
    """严格解析当前 JSON 对象；损坏数据直接报告，不回退旧格式。"""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("memory_metadata_object_required")
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
            base_statement = select(MemoryModel, text_rank.label("search_rank")).where(
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
                project_id = scope_filter.get("project_id")
                base_statement = base_statement.where(
                    _visible_scope_condition(
                        str(scope_filter["session_id"]),
                        project_id if isinstance(project_id, str) else None,
                    )
                )
            rows = (
                await session.execute(
                    base_statement.where(MemoryModel.content_fts.op("@@")(text_query))
                    .order_by(text_rank.desc(), MemoryModel.created_at.desc())
                    .limit(request.limit)
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
        values = [self._domain(row, score=float(score or 0.0)) for row, score in rows]
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
        statement = (
            select(MemoryModel)
            .where(
                MemoryModel.status == MemoryStatus.ACTIVE.value,
                MemoryModel.validity_status == "valid",
                MemoryModel.deleted_time.is_(None),
                *self._unexpired_conditions(),
                _visible_scope_condition(session_id, project_id),
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
            # 查询总数，避免分页时出现不一致
            total = (
                await session.scalar(
                    select(func.count()).select_from(MemoryModel).where(*conditions)
                )
                or 0
            )
            # 查询过期的记忆数量
            expired = (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryModel)
                    .where(*base_conditions, expired_condition)
                )
                or 0
            )
            # 查询最近一周创建的记忆数量
            recent_week = (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryModel)
                    .where(
                        *base_conditions,
                        cast(MemoryModel.created_at, DateTime(timezone=True))
                        >= datetime.now(timezone.utc) - timedelta(days=7),
                    )
                )
                or 0
            )
        values = tuple(self._domain(row) for row in rows)
        return MemoryPage(
            values,
            int(total),
            {
                "total": int(total),
                "expired": int(expired),
                "recent_week": int(recent_week),
            },
        )

    async def _fact_group_rows(
        self, session: AsyncSession, identity: MemoryFact | MemoryFactIdentity
    ) -> list[MemoryModel]:
        """在指定事务中读取当前事实组；撤回仅包含目标值。"""
        rows = (await session.scalars(
            select(MemoryModel).where(
                *_fact_identity_conditions(identity),
                MemoryModel.status == MemoryStatus.ACTIVE.value,
                MemoryModel.validity_status == "valid",
                MemoryModel.deleted_time.is_(None),
            )
        )).all()
        return _matching_fact_rows(rows, identity)

    async def list_fact_group(self, fact: MemoryFact) -> list[MemoryRecord]:
        """读取候选对应的有效事实组。"""
        async with self._session_factory() as session:
            rows = await self._fact_group_rows(session, fact)
        return [self._domain(row) for row in rows]

    async def list_fact_group_by_identity(
        self, identity: MemoryFactIdentity
    ) -> list[MemoryRecord]:
        """读取身份对应的有效事实组。"""
        async with self._session_factory() as session:
            rows = await self._fact_group_rows(session, identity)
        return [self._domain(row) for row in rows]

    async def get(self, memory_id: str):
        async with self._session_factory() as session:
            row = await session.get(MemoryModel, memory_id)
        return None if row is None else self._domain(row)

    async def revisions(self, memory_id: str):
        async with self._session_factory() as session:
            row = await session.get(MemoryModel, memory_id)
            if row is None:
                return []
            logical_id = row.logical_memory_id
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
            other = target_row if relation.source_memory_id == memory_id else source_row
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

    async def apply_resolution(
        self,
        resolution: MemoryResolution,
        *,
        operation_id: str,
        embedding: list[float],
        lease: LeaseGrant,
    ) -> bool:
        """原子提交事实、修订关系和向量；重复稳定 ID 返回 False。

        参数：resolution 是规则决策，embedding 已在事务外生成，lease 属于候选轮次。
        异常：事实组变化抛出 MemoryStateChangedError；遗忘或失去租约阻止提交。
        """
        candidate = resolution.candidate
        if lease.key != LeaseKey("memory_processing", candidate.source_turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        metadata = memory_domain_to_schema(
            MemoryRecordMetadata(
                fact=candidate.fact,
                extra_fields={
                    "session_id": candidate.session_id,
                    "source_turn_id": candidate.source_turn_id,
                    "type": candidate.memory_type,
                    "category": candidate.category,
                    "confidence": candidate.confidence,
                    "source": "extraction",
                },
            )
        ).model_dump(mode="json", exclude_none=True)
        async with self._session_factory() as session:
            async with session.begin():
                # 与项目遗忘保持一致：先锁项目与来源，再锁事实组。
                project_id = (
                    candidate.fact.scope_id
                    if candidate.fact.scope_kind == "project"
                    else None
                )
                await _ensure_project_not_suppressed(session, project_id)
                await _ensure_project_turns_not_suppressed(
                    session, _source_turn_ids(metadata)
                )
                # 这里先加锁，避免并发修改同一事实。
                await session.execute(
                    select(func.pg_advisory_xact_lock(_fact_lock_id(candidate.fact)))
                )
                await self._leases.assert_current_in_transaction(session, lease)
                if await session.get(MemoryModel, operation_id) is not None:
                    return False
                if resolution.action is ResolutionAction.CREATE:
                    await self._create_memory_in_transaction(
                        session,
                        MemoryWriteCommand(
                            content=candidate.content,
                            metadata=metadata,
                            operation_key=operation_id,
                            expected_memory_ids=resolution.expected_memory_ids,
                        ),
                        lease=lease,
                        scope_locked=True,
                    )
                elif (
                    resolution.action is ResolutionAction.REVISE
                    and resolution.target_memory_id
                ):
                    result = await self._revise_in_transaction(
                        session,
                        resolution.target_memory_id,
                        candidate.content,
                        metadata=metadata,
                        operation_key=operation_id,
                        expected_memory_ids=resolution.expected_memory_ids,
                        lease=lease,
                        scope_locked=True,
                    )
                    if result is None:
                        raise MemoryStateChangedError("memory_revision_target_changed")
                else:
                    raise ValueError("memory_resolution_not_writable")
                if resolution.relation is not None:
                    if not resolution.target_memory_id:
                        raise ValueError("memory_relation_target_required")
                    # 关系出错必须同时回滚新事实及旧版本状态。
                    await session.flush()
                    await self._add_relation_in_transaction(
                        session,
                        operation_id,
                        resolution.target_memory_id,
                        resolution.relation,
                        operation_id=operation_id,
                        lease=lease,
                    )
                await session.flush()
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == operation_id)
                    .values(embedding=embedding)
                )
        return True

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        """创建人工记忆；后台来源必须走带租约的独立入口。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self._create_memory_in_transaction(
                    session, command, lease=None
                )

    async def _create_memory_in_transaction(
        self,
        session: AsyncSession,
        command: MemoryWriteCommand,
        *,
        lease: LeaseGrant | None,
        scope_locked: bool = False,
    ) -> str:
        """Create once per operation key; duplicate enqueues stay idempotent."""
        key = (
            command.operation_key or f"memory-{datetime.now(timezone.utc).timestamp()}"
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
        existing = await session.get(MemoryModel, key)
        if existing is not None:
            if existing.status == MemoryStatus.DELETED.value:
                raise MemorySuppressedError("memory_source_forgotten")
            return key
        project_id = (
            typed_metadata.fact.scope_id
            if typed_metadata.fact is not None
            and typed_metadata.fact.scope_kind == "project"
            else (
                str(command.metadata.get("scope_id", ""))
                if command.metadata.get("scope_kind") == "project"
                else None
            )
        )
        if not scope_locked:
            await _ensure_project_not_suppressed(session, project_id)
            await _ensure_project_turns_not_suppressed(session, source_ids)
        if typed_metadata.fact is not None and not scope_locked:
            await session.execute(
                select(func.pg_advisory_xact_lock(_fact_lock_id(typed_metadata.fact)))
            )
            existing = await session.get(MemoryModel, key)
            if existing is not None:
                if existing.status == MemoryStatus.DELETED.value:
                    raise MemorySuppressedError("memory_source_forgotten")
                return key
        await _lock_suppression_sources(session, source_ids, suppression_fingerprint)
        await _ensure_not_suppressed(session, source_ids, suppression_fingerprint)
        if typed_metadata.fact is not None:
            if command.expected_memory_ids is not None:
                current_ids = await self._active_fact_group_ids(
                    session,
                    typed_metadata.fact,
                )
                if current_ids != tuple(sorted(command.expected_memory_ids)):
                    raise MemoryStateChangedError("memory_fact_group_changed")
        if lease is not None:
            await self._leases.assert_current_in_transaction(session, lease)
        elif lease is None and command.metadata.get("source") in {
            "extraction",
        }:
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
                source_turn_id=str(command.metadata.get("source_turn_id", "")) or None,
                last_observed_at=_now(),
                validity_status="valid",
                valid_until=(
                    typed_metadata.fact.valid_until
                    if typed_metadata.fact is not None
                    else None
                ),
                revision=1,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        return key

    async def _active_fact_group_ids(
        self,
        session: AsyncSession,
        fact: MemoryFact,
    ) -> tuple[str, ...]:
        """在写入事务中读取解析器所见的当前事实组。"""
        rows = await self._fact_group_rows(session, fact)
        return tuple(row.id for row in rows)

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
        async with self._session_factory() as session:
            async with session.begin():
                return await self._revise_in_transaction(
                    session,
                    memory_id,
                    content,
                    metadata=metadata,
                    operation_key=operation_key,
                    expected_memory_ids=expected_memory_ids,
                    lease=None,
                )

    async def _revise_in_transaction(
        self,
        session: AsyncSession,
        memory_id: str,
        content: str,
        *,
        metadata=None,
        operation_key=None,
        expected_memory_ids: tuple[str, ...] | None = None,
        lease=None,
        scope_locked: bool = False,
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
        new_id = operation_key or ""
        existing = await session.get(MemoryModel, new_id) if new_id else None
        if existing is not None:
            if existing.status == MemoryStatus.DELETED.value:
                raise MemorySuppressedError("memory_source_forgotten")
            return new_id
        project_id = (
            typed_metadata.fact.scope_id
            if typed_metadata.fact is not None
            and typed_metadata.fact.scope_kind == "project"
            else (
                str(metadata_payload.get("scope_id", ""))
                if metadata_payload.get("scope_kind") == "project"
                else None
            )
        )
        if not scope_locked:
            await _ensure_project_not_suppressed(session, project_id)
            await _ensure_project_turns_not_suppressed(session, source_ids)
        if typed_metadata.fact is not None and not scope_locked:
            await session.execute(
                select(func.pg_advisory_xact_lock(_fact_lock_id(typed_metadata.fact)))
            )
            existing = await session.get(MemoryModel, new_id)
            if existing is not None:
                if existing.status == MemoryStatus.DELETED.value:
                    raise MemorySuppressedError("memory_source_forgotten")
                return new_id
        if typed_metadata.fact is None:
            old_hint = await session.get(MemoryModel, memory_id)
            identity = None if old_hint is None else _row_fact_identity(old_hint)
            if identity is not None:
                await session.execute(
                    select(func.pg_advisory_xact_lock(_identity_lock_id(identity)))
                )
        await _lock_suppression_sources(session, source_ids, suppression_fingerprint)
        await _ensure_not_suppressed(session, source_ids, suppression_fingerprint)
        if typed_metadata.fact is not None:
            if expected_memory_ids is not None:
                current_ids = await self._active_fact_group_ids(
                    session,
                    typed_metadata.fact,
                )
                if current_ids != tuple(sorted(expected_memory_ids)):
                    raise MemoryStateChangedError("memory_fact_group_changed")
        old = await session.scalar(
            select(MemoryModel).where(MemoryModel.id == memory_id).with_for_update()
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
        elif lease is None and metadata_payload.get("source") == "extraction":
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
                memory_type=metadata_payload.get("type", old.memory_type),
                category=metadata_payload.get("category", old.category),
                confidence=metadata_payload.get("confidence", old.confidence),
                source_kind=old.source_kind,
                status=MemoryStatus.ACTIVE.value,
                source_turn_id=str((metadata or {}).get("source_turn_id", "")) or None,
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
        """人工维护关系；后台写入统一使用 apply_resolution。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._add_relation_in_transaction(
                    session,
                    source_memory_id,
                    target_memory_id,
                    kind,
                    operation_id=operation_id,
                    lease=None,
                )

    async def _add_relation_in_transaction(
        self,
        session: AsyncSession,
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
        if lease is not None:
            locked = (
                await session.scalars(
                    select(MemoryModel)
                    .where(MemoryModel.id.in_((source_memory_id, target_memory_id)))
                    .order_by(MemoryModel.id)
                    .with_for_update()
                )
            ).all()
            source = next(
                (row for row in locked if row.id == worker_source_memory_id),
                None,
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
                metadata = self._domain(row).metadata
                source_ids = _source_turn_ids(metadata)
                fingerprint = _suppression_fingerprint(metadata, row.content)
                await _lock_suppression_sources(
                    session, source_ids, EPISODE_SUPPRESSION_FINGERPRINT
                )
                await _lock_suppression_sources(session, source_ids, fingerprint)
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
                    .where(
                        MemoryModel.id.in_(set(memory_ids)),
                        MemoryModel.status == "active",
                        MemoryModel.validity_status != "invalid",
                        MemoryModel.deleted_time.is_(None),
                        *self._unexpired_conditions(),
                    )
                    .values(
                        access_count=func.coalesce(MemoryModel.access_count, 0) + 1,
                        last_accessed_at=_now(),
                    )
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
        metadata = _json(row.metadata_json)
        metadata.update(
            session_id=row.session_id,
            source_turn_id=row.source_turn_id,
            type=row.memory_type,
            category=row.category,
            confidence=row.confidence,
        )
        return MemoryRecord(
            row.id,
            row.content,
            metadata,
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
