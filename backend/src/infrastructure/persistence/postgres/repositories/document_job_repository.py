"""PostgreSQL 队列及文档索引版本的受控发布。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import AttachmentStatus, DocumentJobPort, DocumentJobQueue
from domain.leases import LeaseGrant, LeaseKey, LeaseLostError

from ..models import (
    AttachmentModel,
    ExecutionLeaseModel,
    KnowledgeDocumentJobModel,
)
from .lease_center import PostgresLeaseCenter


def _now() -> str:
    """生成 UTC 持久化时间。"""
    return datetime.now(timezone.utc).isoformat()


class PostgresDocumentJobRepository(DocumentJobPort, DocumentJobQueue):
    """文档业务队列与共享租约中心的事务适配器。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存数据库会话工厂和共享租约中心。"""
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def enqueue(self, attachment_id: str) -> bool:
        """为附件创建队列项，已排队或运行中的任务保持幂等。"""
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.attachment_id == attachment_id)
                    .with_for_update()
                )
                if existing is not None:
                    if existing.status in {"failed", "cancelled"}:
                        existing.status = "queued"
                        existing.available_at = now
                        existing.updated_at = now
                        existing.error_json = None
                        return True
                    return False
                session.add(
                    KnowledgeDocumentJobModel(
                        job_id=uuid4().hex,
                        attachment_id=attachment_id,
                        status="queued",
                        attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return True

    async def cancel(self, attachment_id: str) -> None:
        """撤销附件的当前文档租约并取消未完成作业。"""
        async with self._session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.attachment_id == attachment_id)
                    .with_for_update()
                )
                if job is not None:
                    job.status = "cancelled"
                    job.updated_at = _now()
                attachment = await session.scalar(
                    select(AttachmentModel)
                    .where(AttachmentModel.id == attachment_id)
                    .with_for_update()
                )
                if attachment is None:
                    return
                lease = await session.scalar(
                    select(ExecutionLeaseModel)
                    .where(
                        ExecutionLeaseModel.resource_type == "document_ingestion",
                        ExecutionLeaseModel.resource_id == attachment_id,
                    )
                    .with_for_update()
                )
                if lease is not None and lease.state == "active":
                    await self._leases.revoke_in_transaction(
                        session,
                        LeaseKey("document_ingestion", attachment_id),
                        expected_token=lease.fencing_token,
                    )

    async def claim_next(
        self, *, max_attempts: int = 5, lease_seconds: float = 60.0
    ) -> dict[str, object] | None:
        """原子领取业务作业并获取附件级文档租约。"""
        async with self._session_factory() as session:
            async with session.begin():
                candidates = (
                    await session.scalars(
                        select(KnowledgeDocumentJobModel)
                        .where(
                            KnowledgeDocumentJobModel.status.in_(("queued", "running")),
                            KnowledgeDocumentJobModel.attempt <= max_attempts,
                            KnowledgeDocumentJobModel.available_at <= _now(),
                            ~select(ExecutionLeaseModel.resource_id).where(
                                ExecutionLeaseModel.resource_type == "document_ingestion",
                                ExecutionLeaseModel.resource_id == KnowledgeDocumentJobModel.attachment_id,
                                ExecutionLeaseModel.state.in_(("active", "revoked")),
                                ExecutionLeaseModel.lease_until > func.clock_timestamp(),
                            ).exists(),
                        )
                        .order_by(
                            KnowledgeDocumentJobModel.available_at,
                            KnowledgeDocumentJobModel.job_id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(32)
                    )
                ).all()
                for row in candidates:
                    attachment = await session.scalar(
                        select(AttachmentModel)
                        .where(AttachmentModel.id == row.attachment_id)
                        .with_for_update()
                    )
                    if attachment is None or attachment.deleted_time is not None:
                        row.status = "cancelled"
                        row.updated_at = _now()
                        continue
                    grant = await self._leases.acquire_in_transaction(
                        session,
                        LeaseKey("document_ingestion", row.attachment_id),
                        owner_id=uuid4().hex,
                        lease_seconds=lease_seconds,
                    )
                    if grant is None:
                        continue
                    if row.attempt >= max_attempts:
                        row.status = "failed"
                        row.error_json = json.dumps(
                            {"message": "maximum document ingestion attempts reached"},
                            ensure_ascii=False,
                        )
                        row.updated_at = _now()
                        attachment.status = AttachmentStatus.FAILED.value
                        attachment.error_message = "maximum document ingestion attempts reached"
                        attachment.updated_at = _now()
                        if not await self._leases.finish_in_transaction(session, grant):
                            raise LeaseLostError(grant.key)
                        continue
                    row.status = "running"
                    row.attempt += 1
                    row.updated_at = _now()
                    attachment.status = AttachmentStatus.PROCESSING.value
                    attachment.error_message = None
                    attachment.updated_at = _now()
                    return {
                        "job_id": row.job_id,
                        "attachment_id": row.attachment_id,
                        "attempt": row.attempt,
                        "lease": grant,
                    }
        return None

    async def mark_succeeded(
        self, job_id: str, result: Mapping[str, object], *, lease: LeaseGrant
    ) -> None:
        """校验租约并原子发布有效索引版本、附件终态和队列结果。"""
        revision_id = str(result.get("index_revision_id") or "")
        if not revision_id:
            raise ValueError("document_index_revision_required")
        async with self._session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.job_id == job_id)
                    .with_for_update()
                )
                if job is None:
                    raise LookupError("document_job_not_found")
                if job.status != "running":
                    raise ValueError("document_job_not_running")
                attachment = await session.scalar(
                    select(AttachmentModel)
                    .where(AttachmentModel.id == job.attachment_id)
                    .with_for_update()
                )
                if attachment is None or attachment.deleted_time is not None:
                    raise LookupError("attachment_deleted")
                if lease.key != LeaseKey("document_ingestion", job.attachment_id):
                    raise ValueError("document_ingestion_lease_mismatch")
                await self._leases.assert_current_in_transaction(session, lease)
                metadata = result.get("_parsed_metadata")
                attachment.active_index_revision_id = revision_id
                attachment.status = AttachmentStatus.READY.value
                attachment.error_message = None
                attachment.parsed_metadata_json = json.dumps(
                    metadata if isinstance(metadata, dict) else {}, ensure_ascii=False
                )
                attachment.adapter_name = _optional_string(result.get("_adapter_name"))
                attachment.adapter_version = _optional_string(result.get("_adapter_version"))
                capabilities = result.get("_capabilities")
                attachment.capabilities_json = json.dumps(
                    capabilities if isinstance(capabilities, list) else [],
                    ensure_ascii=False,
                )
                attachment.updated_at = _now()
                job.status = "succeeded"
                job.result_json = json.dumps(
                    {
                        key: value
                        for key, value in result.items()
                        if not key.startswith("_")
                    },
                    default=str,
                    ensure_ascii=False,
                )
                job.error_json = None
                job.updated_at = _now()
                if not await self._leases.finish_in_transaction(session, lease):
                    raise LeaseLostError(lease.key)

    async def mark_failed(
        self, job_id: str, error: str, *, retry: bool, lease: LeaseGrant
    ) -> None:
        """保留已发布索引；失败/退避和租约释放在同一事务完成。"""
        async with self._session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.job_id == job_id)
                    .with_for_update()
                )
                if job is None:
                    raise LookupError("document_job_not_found")
                if job.status != "running":
                    raise ValueError("document_job_not_running")
                attachment = await session.scalar(
                    select(AttachmentModel)
                    .where(AttachmentModel.id == job.attachment_id)
                    .with_for_update()
                )
                if attachment is None:
                    raise LookupError("attachment_not_found")
                if lease.key != LeaseKey("document_ingestion", job.attachment_id):
                    raise ValueError("document_ingestion_lease_mismatch")
                await self._leases.assert_current_in_transaction(session, lease)
                should_retry = retry and attachment.deleted_time is None
                job.status = "queued" if should_retry else "failed"
                job.error_json = json.dumps({"message": error}, ensure_ascii=False)
                if should_retry:
                    delay = min(2 ** max(0, job.attempt - 1), 300)
                    job.available_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    attachment.status = (
                        AttachmentStatus.READY.value
                        if attachment.active_index_revision_id
                        else AttachmentStatus.UPLOADED.value
                    )
                elif attachment.deleted_time is None:
                    attachment.status = AttachmentStatus.FAILED.value
                    attachment.error_message = error
                job.updated_at = _now()
                attachment.updated_at = _now()
                if not await self._leases.finish_in_transaction(session, lease):
                    raise LeaseLostError(lease.key)


def _optional_string(value: object) -> str | None:
    """把可选适配器标识限制为非空字符串。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


__all__ = ["PostgresDocumentJobRepository"]
