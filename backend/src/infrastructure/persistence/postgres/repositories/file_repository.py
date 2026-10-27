"""新附件和文件分块 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import (
    Attachment,
    AttachmentRepository,
    FileChunk,
    FileChunkRepository,
)
from domain.files.entities import AttachmentStatus
from domain.leases import LeaseGrant, LeaseKey
from ..models import ExecutionLeaseModel, FileChunkModel, KnowledgeDocumentJobModel

from ..models import AttachmentModel
from ..mappers.converters import (
    attachment_domain_to_model,
    attachment_model_to_domain,
    file_chunk_domain_to_model,
    file_chunk_model_to_domain,
)
from .lease_center import PostgresLeaseCenter


class PostgresAttachmentRepository(AttachmentRepository):
    """通过注入会话工厂管理附件元数据。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def create(self, attachment: Attachment) -> Attachment:
        """创建附件记录。"""
        async with self._session_factory() as db:
            async with db.begin():
                db.add(attachment_domain_to_model(attachment))
        return attachment

    async def get(
        self, attachment_id: str, *, session_id: str | None = None
    ) -> Attachment | None:
        """查询未删除附件，并可按会话限制访问范围。"""
        async with self._session_factory() as db:
            statement = select(AttachmentModel).where(
                AttachmentModel.id == attachment_id,
                AttachmentModel.deleted_time.is_(None),
            )
            if session_id is not None:
                statement = statement.where(AttachmentModel.session_id == session_id)
            row = (await db.execute(statement)).scalar_one_or_none()
        return None if row is None else attachment_model_to_domain(row)

    async def list_by_session(self, session_id: str) -> list[Attachment]:
        """列出会话附件。"""
        return await self._list(AttachmentModel.session_id == session_id)

    async def list_all_attachments(self) -> list[Attachment]:
        """列出所有未删除附件，供跨知识库召回使用。"""
        return await self._list(AttachmentModel.id.is_not(None))

    async def list_by_knowledge_base(self, knowledge_base_id: str) -> list[Attachment]:
        """列出知识库文档。"""
        return await self._list(AttachmentModel.knowledge_base_id == knowledge_base_id)

    async def save(self, attachment: Attachment) -> Attachment:
        """更新附件状态和处理元数据。"""
        async with self._session_factory() as db:
            async with db.begin():
                await db.execute(
                    update(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment.id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .values(
                        **{
                            "status": attachment.status.value,
                            "adapter_name": attachment.adapter_name,
                            "adapter_version": attachment.adapter_version,
                            "capabilities_json": attachment_domain_to_model(
                                attachment
                            ).capabilities_json,
                            "parsed_metadata_json": attachment_domain_to_model(
                                attachment
                            ).parsed_metadata_json,
                            "error_message": attachment.error_message,
                            "updated_at": attachment.updated_at.isoformat(),
                        }
                    )
                )
        return attachment

    async def soft_delete(self, attachment_id: str, *, session_id: str) -> bool:
        """按会话所有权软删除附件。"""
        deleted_at = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as db:
            async with db.begin():
                job = await db.scalar(
                    select(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.attachment_id == attachment_id)
                    .with_for_update()
                )
                attachment = await db.scalar(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.session_id == session_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if attachment is None:
                    return False
                if job is not None:
                    job.status = "cancelled"
                    job.updated_at = deleted_at
                result = await db.execute(
                    update(AttachmentModel)
                    .where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.session_id == session_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .returning(AttachmentModel.id)
                    .values(
                        status=AttachmentStatus.DELETED.value,
                        deleted_time=deleted_at,
                        updated_at=deleted_at,
                    )
                )
                lease = await db.scalar(
                    select(ExecutionLeaseModel)
                    .where(
                        ExecutionLeaseModel.resource_type == "document_ingestion",
                        ExecutionLeaseModel.resource_id == attachment_id,
                    )
                    .with_for_update()
                )
                if lease is not None and lease.state == "active":
                    await self._leases.revoke_in_transaction(
                        db,
                        LeaseKey("document_ingestion", attachment_id),
                        expected_token=lease.fencing_token,
                    )
        return result.scalar_one_or_none() is not None

    async def _list(self, *conditions) -> list[Attachment]:
        """按条件列出未删除附件。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(AttachmentModel)
                .where(*conditions, AttachmentModel.deleted_time.is_(None))
                .order_by(AttachmentModel.created_at.asc())
            )
            rows = result.scalars().all()
        return [attachment_model_to_domain(row) for row in rows]


class PostgresFileChunkRepository(FileChunkRepository):
    """通过注入会话工厂管理文件分块。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def replace_for_attachment(
        self, attachment_id: str, chunks: list[FileChunk], *, lease: LeaseGrant
    ) -> None:
        """写入当前执行的隔离索引版本，不触碰已发布版本。"""
        if lease.key != LeaseKey("document_ingestion", attachment_id):
            raise ValueError("document_ingestion_lease_mismatch")
        if any(chunk.attachment_id != attachment_id for chunk in chunks):
            raise ValueError("document_chunk_attachment_mismatch")
        async with self._session_factory() as db:
            async with db.begin():
                attachment = await db.scalar(
                    select(AttachmentModel)
                    .where(AttachmentModel.id == attachment_id)
                    .with_for_update()
                )
                if attachment is None or attachment.deleted_time is not None:
                    raise LookupError("attachment_deleted")
                await self._leases.assert_current_in_transaction(db, lease)
                db.add_all([file_chunk_domain_to_model(chunk) for chunk in chunks])

    async def list_by_attachment(self, attachment_id: str) -> list[FileChunk]:
        """按分块序号读取未删除分块。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(FileChunkModel)
                .join(AttachmentModel, AttachmentModel.id == FileChunkModel.attachment_id)
                .where(
                    FileChunkModel.attachment_id == attachment_id,
                    FileChunkModel.deleted_time.is_(None),
                    FileChunkModel.index_revision_id == AttachmentModel.active_index_revision_id,
                    AttachmentModel.deleted_time.is_(None),
                )
                .order_by(FileChunkModel.ordinal.asc())
            )
            rows = result.scalars().all()
        return [file_chunk_model_to_domain(row) for row in rows]

    async def filter_active_index_revisions(
        self, candidates: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """批量过滤 Neo4j 候选，只保留附件当前发布的索引代次。"""
        keys = {
            (str(row.get("attachment_id") or ""), str(row.get("index_revision_id") or ""))
            for row in candidates
        }
        keys.discard(("", ""))
        if not keys:
            return []
        attachment_ids = {attachment_id for attachment_id, _ in keys}
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(AttachmentModel.id, AttachmentModel.active_index_revision_id)
                    .where(
                        AttachmentModel.id.in_(attachment_ids),
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).all()
        active = {(row.id, row.active_index_revision_id) for row in rows}
        return [
            row
            for row in candidates
            if (str(row.get("attachment_id") or ""), str(row.get("index_revision_id") or "")) in active
        ]
