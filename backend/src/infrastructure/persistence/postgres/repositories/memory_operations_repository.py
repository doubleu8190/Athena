"""PostgreSQL 记忆提取清单与写入回执。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from domain.leases import LeaseGrant, LeaseKey

from domain.memory import (
    MemoryCandidateSnapshot,
    MemoryFactExtractionSnapshot,
    MemoryOperationsPort,
    MemoryResolutionProposal,
    MemoryWriteOperation,
)
from shared.contracts.memory import (
    MemoryCandidateSnapshotSchema,
    MemoryFactExtractionSnapshotSchema,
    MemoryResolutionProposalSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)

from ..models import MemoryExtractionSnapshotModel, MemoryWriteOperationModel
from .lease_center import PostgresLeaseCenter


def _now() -> str:
    """生成 UTC 持久化时间。"""
    return datetime.now(timezone.utc).isoformat()


class PostgresMemoryOperationsRepository(MemoryOperationsPort):
    """以新增表保存冻结候选和可重试写入回执。"""

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

    async def begin_operation(
        self, operation_id: str, turn_id: str, candidate: MemoryCandidateSnapshot,
        *, lease: LeaseGrant,
    ) -> MemoryWriteOperation:
        """创建或读取同一候选的写入回执。"""
        if lease.key != LeaseKey("memory_processing", turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        candidate_json = memory_domain_to_schema(candidate).model_dump_json()
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(MemoryWriteOperationModel)
                    .values(
                        operation_id=operation_id,
                        turn_id=turn_id,
                        candidate_json=candidate_json,
                        status="pending",
                        projection_status="pending",
                        created_at=_now(),
                        updated_at=_now(),
                    )
                    .on_conflict_do_nothing(index_elements=["operation_id"])
                )
                row = await session.scalar(
                    select(MemoryWriteOperationModel)
                    .where(MemoryWriteOperationModel.operation_id == operation_id)
                    .with_for_update()
                )
                if row is None:
                    raise RuntimeError("memory_write_operation_not_saved")
                await self._leases.assert_current_in_transaction(session, lease)
                stored_candidate = memory_schema_to_domain(
                    MemoryCandidateSnapshotSchema.model_validate_json(
                        row.candidate_json
                    )
                )
                if row.turn_id != turn_id or stored_candidate != candidate:
                    raise ValueError("memory_write_operation_identity_mismatch")
                return self._operation(row, stored_candidate)

    async def complete_operation(
        self,
        operation_id: str,
        *,
        status: str,
        decision: MemoryResolutionProposal,
        result_memory_id: str | None,
        projection_status: str,
        lease: LeaseGrant,
    ) -> MemoryWriteOperation:
        """记录候选决策与结果；已完成回执不被重试覆盖。"""
        if status not in {"applied", "ignored", "deferred", "rejected", "skipped"}:
            raise ValueError("memory_write_operation_terminal_status_invalid")
        if projection_status not in {"not_required", "pending", "ready", "failed"}:
            raise ValueError("memory_projection_status_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryWriteOperationModel)
                    .where(MemoryWriteOperationModel.operation_id == operation_id)
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("memory_write_operation_not_found")
                if lease.key != LeaseKey("memory_processing", row.turn_id):
                    raise ValueError("memory_processing_lease_mismatch")
                await self._leases.assert_current_in_transaction(session, lease)
                candidate = memory_schema_to_domain(
                    MemoryCandidateSnapshotSchema.model_validate_json(
                        row.candidate_json
                    )
                )
                if row.status == "pending":
                    row.status = status
                    if row.decision_json is None:
                        row.decision_json = memory_domain_to_schema(
                            decision
                        ).model_dump_json()
                    row.result_memory_id = result_memory_id
                    row.projection_status = projection_status
                    row.updated_at = _now()
                return self._operation(row, candidate)

    async def record_decision(
        self, operation_id: str, decision: MemoryResolutionProposal,
        *, lease: LeaseGrant,
    ) -> MemoryWriteOperation:
        """在产生记忆副作用前保存已经校验的决策。"""
        decision_json = memory_domain_to_schema(decision).model_dump_json()
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryWriteOperationModel)
                    .where(MemoryWriteOperationModel.operation_id == operation_id)
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("memory_write_operation_not_found")
                if lease.key != LeaseKey("memory_processing", row.turn_id):
                    raise ValueError("memory_processing_lease_mismatch")
                await self._leases.assert_current_in_transaction(session, lease)
                if row.status == "pending":
                    if row.decision_json not in {None, decision_json}:
                        raise ValueError("memory_write_operation_decision_changed")
                    row.decision_json = decision_json
                    row.updated_at = _now()
                candidate = memory_schema_to_domain(
                    MemoryCandidateSnapshotSchema.model_validate_json(
                        row.candidate_json
                    )
                )
                return self._operation(row, candidate)

    async def clear_pending_decision(
        self, operation_id: str, *, lease: LeaseGrant
    ) -> None:
        """清除因目标版本并发变化而失效的待提交决策。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryWriteOperationModel)
                    .where(MemoryWriteOperationModel.operation_id == operation_id)
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("memory_write_operation_not_found")
                if lease.key != LeaseKey("memory_processing", row.turn_id):
                    raise ValueError("memory_processing_lease_mismatch")
                await self._leases.assert_current_in_transaction(session, lease)
                if row.status == "pending":
                    row.decision_json = None
                    row.updated_at = _now()

    async def skip_pending_turn(self, turn_id: str, *, lease: LeaseGrant) -> int:
        """将设置关闭时尚未提交的操作终结为 skipped。"""
        if lease.key != LeaseKey("memory_processing", turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(MemoryWriteOperationModel)
                        .where(
                            MemoryWriteOperationModel.turn_id == turn_id,
                            MemoryWriteOperationModel.status == "pending",
                        )
                        .order_by(MemoryWriteOperationModel.operation_id)
                        .with_for_update()
                    )
                ).all()
                await self._leases.assert_current_in_transaction(session, lease)
                for row in rows:
                    row.status = "skipped"
                    row.projection_status = "not_required"
                    row.updated_at = _now()
                return len(rows)

    @staticmethod
    def _operation(
        row: MemoryWriteOperationModel, candidate: MemoryCandidateSnapshot
    ) -> MemoryWriteOperation:
        """在数据库边界恢复严格操作契约。"""
        return MemoryWriteOperation(
            operation_id=row.operation_id,
            turn_id=row.turn_id,
            candidate=candidate,
            status=row.status,
            decision=(
                None
                if row.decision_json is None
                else memory_schema_to_domain(
                    MemoryResolutionProposalSchema.model_validate_json(
                        row.decision_json
                    )
                )
            ),
            result_memory_id=row.result_memory_id,
            projection_status=row.projection_status,
        )


__all__ = ["PostgresMemoryOperationsRepository"]
