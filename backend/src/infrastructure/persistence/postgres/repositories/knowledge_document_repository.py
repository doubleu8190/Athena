"""知识库文档 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from domain.leases import LeaseKey

from domain.files import Attachment, AttachmentStatus
from domain.knowledge import KnowledgeDocumentRepository

from ..models import (
    AttachmentModel,
    FileArtifactModel,
    FileChunkModel,
    KnowledgeDocumentJobModel,
)
from ..mappers.converters import attachment_domain_to_model, attachment_model_to_domain
from .lease_center import PostgresLeaseCenter


class PostgresKnowledgeDocumentRepository(KnowledgeDocumentRepository):
    """知识库文档的版本、列表和软删除操作。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def create(self, attachment: Attachment) -> Attachment:
        async with self._session_factory() as db:
            async with db.begin():
                if attachment.document_version > 1:
                    old_ids = list(
                        (
                            await db.scalars(
                                select(AttachmentModel.id)
                                .where(
                                    AttachmentModel.logical_document_id == attachment.logical_document_id,
                                    AttachmentModel.knowledge_base_id == attachment.knowledge_base_id,
                                    AttachmentModel.deleted_time.is_(None),
                                )
                                .order_by(AttachmentModel.id)
                            )
                        ).all()
                    )
                    if old_ids:
                        old_jobs = (
                            await db.scalars(
                                select(KnowledgeDocumentJobModel)
                                .where(KnowledgeDocumentJobModel.attachment_id.in_(old_ids))
                                .order_by(KnowledgeDocumentJobModel.attachment_id)
                                .with_for_update()
                            )
                        ).all()
                        for job in old_jobs:
                            job.status = "cancelled"
                            job.updated_at = attachment.created_at.isoformat()
                        old_rows = (
                            await db.scalars(
                                select(AttachmentModel)
                                .where(AttachmentModel.id.in_(old_ids))
                                .order_by(AttachmentModel.id)
                                .with_for_update()
                            )
                        ).all()
                        for old in old_rows:
                            lease = await self._leases.get_in_transaction(
                                db, LeaseKey("document_ingestion", old.id)
                            )
                            if lease is not None and lease.state == "active" and lease.fencing_token is not None:
                                await self._leases.revoke_in_transaction(
                                    db,
                                    LeaseKey("document_ingestion", old.id),
                                    expected_token=lease.fencing_token,
                                )
                    await db.execute(
                        update(AttachmentModel)
                        .where(
                            AttachmentModel.logical_document_id
                            == attachment.logical_document_id,
                            AttachmentModel.knowledge_base_id
                            == attachment.knowledge_base_id,
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .values(deleted_time=attachment.created_at.isoformat())
                    )
                db.add(attachment_domain_to_model(attachment))
        return attachment

    async def get(
        self, attachment_id: str, *, knowledge_base_id: str
    ) -> Attachment | None:
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else attachment_model_to_domain(row)

    async def list_documents(self, knowledge_base_id: str) -> list[Attachment]:
        async with self._session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                            AttachmentModel.deleted_time.is_(None),
                        )
                        .order_by(AttachmentModel.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [attachment_model_to_domain(row) for row in rows]

    async def list_versions(
        self, attachment_id: str, *, knowledge_base_id: str
    ) -> list[Attachment]:
        async with self._session_factory() as db:
            target = (
                await db.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.id == attachment_id,
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                return []
            rows = (
                (
                    await db.execute(
                        select(AttachmentModel)
                        .where(
                            AttachmentModel.logical_document_id
                            == target.logical_document_id,
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                        )
                        .order_by(AttachmentModel.document_version.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [attachment_model_to_domain(row) for row in rows]

    async def latest_for_filename(
        self, filename: str, *, knowledge_base_id: str
    ) -> Attachment | None:
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.filename == filename,
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .order_by(AttachmentModel.document_version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return None if row is None else attachment_model_to_domain(row)

    async def soft_delete(self, attachment_id: str, *, knowledge_base_id: str) -> bool:
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
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
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
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                    .values(
                        status=AttachmentStatus.DELETED.value,
                        deleted_time=deleted_at,
                        updated_at=deleted_at,
                    )
                    .returning(AttachmentModel.id)
                )
                if result.scalar_one_or_none() is None:
                    return False
                lease = await self._leases.get_in_transaction(
                    db, LeaseKey("document_ingestion", attachment_id)
                )
                if lease is not None and lease.state == "active" and lease.fencing_token is not None:
                    await self._leases.revoke_in_transaction(
                        db,
                        LeaseKey("document_ingestion", attachment_id),
                        expected_token=lease.fencing_token,
                    )
                await db.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(deleted_time=deleted_at)
                )
                await db.execute(
                    delete(FileArtifactModel).where(
                        FileArtifactModel.attachment_id == attachment_id
                    )
                )
        return True


__all__ = ["PostgresKnowledgeDocumentRepository"]
