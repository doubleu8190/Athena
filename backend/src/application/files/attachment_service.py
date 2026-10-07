"""附件上传、查询和删除用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from domain.files import (
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileMetadata,
    FileStoragePort,
)
from domain.sessions import SessionRepository


class AttachmentNotFoundError(LookupError):
    """附件不存在或不属于目标会话。"""


class SessionNotFoundError(LookupError):
    """目标会话不存在。"""


class AttachmentService:
    """协调会话附件的校验、存储、持久化和异步处理入队。"""

    def __init__(
        self,
        sessions: SessionRepository,
        attachments: AttachmentRepository,
        storage: FileStoragePort,
        adapters: FileAdapterRegistryPort,
        jobs: DocumentJobPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._attachments = attachments
        self._storage = storage
        self._adapters = adapters
        self._jobs = jobs
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_for_session(self, session_id: str) -> list[Attachment]:
        await self._require_session(session_id)
        return await self._attachments.list_by_session(session_id)

    async def get(self, session_id: str, attachment_id: str) -> Attachment:
        await self._require_session(session_id)
        value = await self._attachments.get(attachment_id, session_id=session_id)
        if value is None:
            raise AttachmentNotFoundError(attachment_id)
        return value

    async def supported_extensions(self, session_id: str) -> list[str]:
        await self._require_session(session_id)
        return self._adapters.supported_extensions()

    async def upload(
        self,
        session_id: str,
        *,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
        message_id: str | None = None,
    ) -> Attachment:
        await self._require_session(session_id)
        safe_name = self._normalize_filename(filename)
        adapter = self._adapters.select(safe_name, mime_type)
        stored = await self._storage.save_stream(chunks)
        now = self._clock()
        attachment = Attachment(
            id=self._id_factory(),
            logical_document_id=self._id_factory(),
            document_version=1,
            session_id=session_id,
            knowledge_base_id=None,
            message_id=message_id,
            filename=safe_name,
            mime_type=mime_type or "application/octet-stream",
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            status=AttachmentStatus.UPLOADED,
            capabilities=adapter.capabilities,
            metadata=FileMetadata(),
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        try:
            saved = await self._attachments.create(attachment)
            await self._jobs.enqueue(saved.id)
            return saved
        except Exception:
            await self._compensate(attachment)
            raise

    async def delete(self, session_id: str, attachment_id: str) -> None:
        await self.get(session_id, attachment_id)
        await self._jobs.cancel(attachment_id)
        if not await self._attachments.soft_delete(attachment_id, session_id=session_id):
            raise AttachmentNotFoundError(attachment_id)

    async def _require_session(self, session_id: str) -> None:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        normalized = PurePosixPath(filename.replace("\\", "/")).name
        if not normalized or "\x00" in normalized or normalized in {".", ".."}:
            raise ValueError("invalid filename")
        return normalized

    async def _compensate(self, attachment: Attachment) -> None:
        try:
            await self._jobs.cancel(attachment.id)
        finally:
            try:
                await self._attachments.soft_delete(
                    attachment.id, session_id=attachment.session_id or ""
                )
            finally:
                await self._storage.discard(attachment.storage_key)
