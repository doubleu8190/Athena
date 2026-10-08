"""知识库和文档写入用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileMetadata,
    FileStoragePort,
    KnowledgeBase,
    KnowledgeBaseRepository,
    KnowledgeDocumentRepository,
)

from .document_service import KnowledgeDocumentService
from .errors import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError


class KnowledgeBaseService:
    """协调知识库元数据和文档上传生命周期。"""

    def __init__(
        self,
        repositories: KnowledgeBaseRepository,
        documents: KnowledgeDocumentRepository,
        storage: FileStoragePort,
        adapters: FileAdapterRegistryPort,
        jobs: DocumentJobPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repositories = repositories
        self._documents = documents
        self._storage = storage
        self._adapters = adapters
        self._jobs = jobs
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._document_service = KnowledgeDocumentService(
            repositories,
            documents,
            storage,
            adapters,
            jobs,
            id_factory=id_factory,
            clock=self._clock,
        )

    async def list(self) -> list[KnowledgeBase]:
        return await self._repositories.list_all()

    async def get(self, knowledge_base_id: str) -> KnowledgeBase:
        value = await self._repositories.get(knowledge_base_id)
        if value is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        return value

    async def create(self, name: str, description: str = "") -> KnowledgeBase:
        normalized = name.strip()
        if not normalized:
            raise ValueError("knowledge base name must not be empty")
        now = self._clock()
        return await self._repositories.create(
            KnowledgeBase(
                id=self._id_factory(),
                name=normalized,
                description=description.strip(),
                document_count=0,
                ready_document_count=0,
                total_size_bytes=0,
                created_at=now,
                updated_at=now,
            )
        )

    async def update(self, knowledge_base_id: str, name: str, description: str = "") -> KnowledgeBase:
        current = await self.get(knowledge_base_id)
        normalized = name.strip()
        if not normalized:
            raise ValueError("knowledge base name must not be empty")
        updated = KnowledgeBase(
            id=current.id,
            name=normalized,
            description=description.strip(),
            document_count=current.document_count,
            ready_document_count=current.ready_document_count,
            total_size_bytes=current.total_size_bytes,
            created_at=current.created_at,
            updated_at=self._clock(),
        )
        return await self._repositories.save(updated)

    async def delete(self, knowledge_base_id: str) -> None:
        await self.get(knowledge_base_id)
        for document in await self._documents.list(knowledge_base_id):
            await self._jobs.cancel(document.id)
            await self._documents.soft_delete(document.id, knowledge_base_id=knowledge_base_id)
        if not await self._repositories.delete(knowledge_base_id):
            raise KnowledgeBaseNotFoundError(knowledge_base_id)

    async def upload_document(
        self,
        knowledge_base_id: str,
        *,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
    ) -> Attachment:
        return await self._document_service.upload_document(
            knowledge_base_id,
            filename=filename,
            mime_type=mime_type,
            chunks=chunks,
        )

    async def list_documents(self, knowledge_base_id: str) -> list[Attachment]:
        return await self._document_service.list_documents(knowledge_base_id)

    async def list_versions(self, knowledge_base_id: str, attachment_id: str) -> list[Attachment]:
        return await self._document_service.list_versions(knowledge_base_id, attachment_id)

    async def delete_document(self, knowledge_base_id: str, attachment_id: str) -> None:
        await self._document_service.delete_document(knowledge_base_id, attachment_id)

    async def supported_extensions(self) -> list[str]:
        return await self._document_service.supported_extensions()

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        value = PurePosixPath(filename.replace("\\", "/")).name
        if not value or "\x00" in value or value in {".", ".."}:
            raise ValueError("invalid filename")
        return value
