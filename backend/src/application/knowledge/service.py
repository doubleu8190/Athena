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
)
from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository, KnowledgeDocumentRepository

from .document_service import KnowledgeDocumentService
from .errors import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError
from application.common.responses import AttachmentResponse, KnowledgeBaseResponse


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

    async def list_knowledge_bases(self) -> list[KnowledgeBaseResponse]:
        return [_kb_response(value) for value in await self._repositories.list_knowledge_bases()]

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        value = await self._repositories.get(knowledge_base_id)
        if value is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
        return _kb_response(value)

    async def create_knowledge_base(self, name: str, description: str = "") -> KnowledgeBaseResponse:
        normalized = name.strip()
        if not normalized:
            raise ValueError("knowledge base name must not be empty")
        now = self._clock()
        return _kb_response(await self._repositories.create(
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
        ))

    async def update_knowledge_base(self, knowledge_base_id: str, name: str, description: str = "") -> KnowledgeBase:
        current = await self._repositories.get(knowledge_base_id)
        if current is None:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)
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
        return _kb_response(await self._repositories.save(updated))

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        await self.get(knowledge_base_id)
        for document in await self._documents.list_documents(knowledge_base_id):
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
    ) -> AttachmentResponse:
        return _attachment_response(await self._document_service.upload_document(
            knowledge_base_id,
            filename=filename,
            mime_type=mime_type,
            chunks=chunks,
        ))

    async def list_documents(self, knowledge_base_id: str) -> list[AttachmentResponse]:
        return [_attachment_response(value) for value in await self._document_service.list_documents(knowledge_base_id)]

    async def list_versions(self, knowledge_base_id: str, attachment_id: str) -> list[AttachmentResponse]:
        return [_attachment_response(value) for value in await self._document_service.list_versions(knowledge_base_id, attachment_id)]

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


def _kb_response(value: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库实体转换为只读应用响应。"""
    return KnowledgeBaseResponse(
        id=value.id,
        name=value.name,
        description=value.description,
        document_count=value.document_count,
        ready_document_count=value.ready_document_count,
        total_size_bytes=value.total_size_bytes,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _attachment_response(value: Attachment) -> AttachmentResponse:
    """将知识库附件实体转换为只读应用响应。"""
    return AttachmentResponse(
        id=value.id,
        filename=value.filename,
        mime_type=value.mime_type,
        size_bytes=value.size_bytes,
        sha256=value.sha256,
        storage_key=value.storage_key,
        status=value.status.value,
        session_id=value.session_id,
        knowledge_base_id=value.knowledge_base_id,
        message_id=value.message_id,
        adapter_name=value.adapter_name,
        adapter_version=value.adapter_version,
        capabilities=tuple(value.capabilities),
        metadata=dict(value.metadata.values),
        error_message=value.error_message,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )
