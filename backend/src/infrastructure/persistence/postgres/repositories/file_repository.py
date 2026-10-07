"""新附件和文件分块 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import Attachment, FileChunk
from domain.files.entities import AttachmentStatus
from ..models import FileChunkModel

from ..models import AttachmentModel
from .converters import (
    attachment_domain_to_model,
    attachment_model_to_domain,
    file_chunk_domain_to_model,
    file_chunk_model_to_domain,
)


class PostgresAttachmentRepository:
    """通过注入会话工厂管理附件元数据。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

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


class PostgresFileChunkRepository:
    """通过注入会话工厂管理文件分块。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def replace_for_attachment(
        self, attachment_id: str, chunks: list[FileChunk]
    ) -> None:
        """软删除旧分块并写入当前分块集合。"""
        deleted_at = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as db:
            async with db.begin():
                await db.execute(
                    update(FileChunkModel)
                    .where(
                        FileChunkModel.attachment_id == attachment_id,
                        FileChunkModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=deleted_at)
                )
                db.add_all([file_chunk_domain_to_model(chunk) for chunk in chunks])

    async def list_by_attachment(self, attachment_id: str) -> list[FileChunk]:
        """按分块序号读取未删除分块。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(FileChunkModel)
                .where(
                    FileChunkModel.attachment_id == attachment_id,
                    FileChunkModel.deleted_time.is_(None),
                )
                .order_by(FileChunkModel.ordinal.asc())
            )
            rows = result.scalars().all()
        return [file_chunk_model_to_domain(row) for row in rows]
