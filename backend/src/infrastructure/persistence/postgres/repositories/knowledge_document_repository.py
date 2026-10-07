"""知识库文档 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import Attachment, AttachmentStatus

from ..models import (
    AttachmentModel,
    FileArtifactModel,
    FileChunkModel,
)
from .converters import attachment_domain_to_model, attachment_model_to_domain


class PostgresKnowledgeDocumentRepository:
    """知识库文档的版本、列表和软删除操作。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, attachment: Attachment) -> Attachment:
        async with self._session_factory() as db:
            async with db.begin():
                if attachment.document_version > 1:
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

    async def list(self, knowledge_base_id: str) -> list[Attachment]:
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
