"""PostgreSQL 冻结事实提取清单。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from domain.leases import LeaseGrant, LeaseKey

from domain.memory import (
    MemoryFactExtractionSnapshot,
    MemorySnapshotPort,
    MemorySuppressedError,
)
from application.contracts.memory import (
    MemoryFactExtractionSnapshotSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)

from .lease_center import PostgresLeaseCenter
from .memory_suppression import project_turn_is_writable
from ..models import MemoryExtractionSnapshotModel


def _now() -> str:
    """生成 UTC 持久化时间。"""
    return datetime.now(timezone.utc).isoformat()


class PostgresMemorySnapshotRepository(MemorySnapshotPort):
    """按轮次保存唯一的冻结事实候选。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存数据库会话工厂。"""
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def get_extraction_snapshot(
        self, turn_id: str
    ) -> MemoryFactExtractionSnapshot | None:
        """读取轮次冻结清单；不存在时返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(MemoryExtractionSnapshotModel, turn_id)
        if row is None:
            return None
        return memory_schema_to_domain(
            MemoryFactExtractionSnapshotSchema.model_validate_json(row.snapshot_json)
        )

    async def save_extraction_snapshot(
        self, snapshot: MemoryFactExtractionSnapshot, *, lease: LeaseGrant
    ) -> MemoryFactExtractionSnapshot:
        """以 turn_id 首写胜出方式保存提取结果并返回最终快照。"""
        if lease.key != LeaseKey("memory_processing", snapshot.turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                # 与项目遗忘使用同一来源锁，防止远端提取返回后恢复已删除快照。
                if not await project_turn_is_writable(session, snapshot.turn_id):
                    raise MemorySuppressedError("memory_project_turn_forgotten")
                await session.execute(
                    pg_insert(MemoryExtractionSnapshotModel)
                    .values(
                        turn_id=snapshot.turn_id,
                        snapshot_json=memory_domain_to_schema(snapshot).model_dump_json(),
                        created_at=_now(),
                    )
                    .on_conflict_do_nothing(index_elements=["turn_id"])
                )
                row = await session.scalar(
                    select(MemoryExtractionSnapshotModel)
                    .where(MemoryExtractionSnapshotModel.turn_id == snapshot.turn_id)
                    .with_for_update()
                )
                if row is None:
                    raise RuntimeError("memory_extraction_snapshot_not_saved")
                await self._leases.assert_current_in_transaction(session, lease)
                return memory_schema_to_domain(
                    MemoryFactExtractionSnapshotSchema.model_validate_json(
                        row.snapshot_json
                    )
                )



__all__ = ["PostgresMemorySnapshotRepository"]
