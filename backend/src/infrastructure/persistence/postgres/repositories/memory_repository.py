"""PostgreSQL long-term memory implementation."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryWriteCommand,
)
from domain.memory.repository_ports import MemoryRepositoryPort

from ..models import (
    MemoryModel,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed

class PostgresMemoryRepository(MemoryRepositoryPort):
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def initialize(self) -> None:
        return None

    async def search(self, request: MemorySearchRequest):
        async with self._session_factory() as session:
            filters = request.filters or {}
            statement = select(MemoryModel).where(
                MemoryModel.content.ilike(f"%{request.query}%"),
                MemoryModel.status == MemoryStatus.ACTIVE.value,
                MemoryModel.validity_status != "invalid",
                MemoryModel.deleted_time.is_(None),
                *self._unexpired_conditions(),
            )
            if filters.get("session_id"):
                statement = statement.where(
                    MemoryModel.session_id == filters["session_id"]
                )
            rows = (
                (await session.execute(statement.limit(request.limit)))
                .scalars()
                .all()
            )
        values = [self._domain(row, score=1.0) for row in rows]
        if request.record_access:
            await self.record_access([value.id for value in values])
        return values

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

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        """Create once per operation key; duplicate enqueues stay idempotent."""
        key = (
            command.operation_key
            or f"memory-{datetime.now(timezone.utc).timestamp()}"
        )
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(MemoryModel, key)
                if existing is not None:
                    return key
                session.add(
                    MemoryModel(
                        id=key,
                        logical_memory_id=key,
                        session_id=str(command.metadata.get("session_id", "")),
                        content=command.content,
                        metadata_json=json.dumps(
                            command.metadata, ensure_ascii=False
                        ),
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
                )
        return key

    async def revise(
        self, memory_id: str, content: str, *, metadata=None, operation_key=None
    ):
        """Insert a new immutable revision and supersede the previous row."""
        async with self._session_factory() as session:
            async with session.begin():
                old = await session.get(MemoryModel, memory_id)
                if old is None or old.status != MemoryStatus.ACTIVE.value:
                    return None
                new_id = (
                    operation_key
                    or f"{old.logical_memory_id}:rev:{old.revision + 1}"
                )
                if await session.get(MemoryModel, new_id) is not None:
                    return new_id
                old.status = MemoryStatus.SUPERSEDED.value
                session.add(
                    MemoryModel(
                        id=new_id,
                        logical_memory_id=old.logical_memory_id,
                        session_id=old.session_id,
                        content=content,
                        metadata_json=json.dumps(
                            metadata or {}, ensure_ascii=False
                        ),
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
                        validity_status=old.validity_status,
                        valid_until=old.valid_until,
                        expires_at=old.expires_at,
                        revision=old.revision + 1,
                    )
                )
        return new_id

    async def update_validity(
        self, memory_id: str, validity_status: str, *, valid_until=None
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(validity_status=validity_status, valid_until=valid_until)
                )
        return bool(result.rowcount)

    async def delete(self, memory_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(status=MemoryStatus.DELETED.value, deleted_time=_now())
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
