"""文件与知识库持久化端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from .entities import (
    AdapterInfo,
    Attachment,
    FileArtifact,
    FileChunk,
    FileMetadata,
    KnowledgeBase,
)


class AttachmentRepository(Protocol):
    """附件持久化能力。"""

    async def create(self, attachment: Attachment) -> Attachment: ...

    async def get(self, attachment_id: str, *, session_id: str | None = None) -> Attachment | None: ...

    async def list_by_session(self, session_id: str) -> list[Attachment]: ...

    async def list_by_knowledge_base(self, knowledge_base_id: str) -> list[Attachment]: ...

    async def save(self, attachment: Attachment) -> Attachment: ...

    async def soft_delete(self, attachment_id: str, *, session_id: str) -> bool: ...


class FileChunkRepository(Protocol):
    """文件分块持久化能力。"""

    async def replace_for_attachment(self, attachment_id: str, chunks: list[FileChunk]) -> None: ...

    async def list_by_attachment(self, attachment_id: str) -> list[FileChunk]: ...


class FileArtifactRepository(Protocol):
    """文件解析产物持久化能力。"""

    async def get(self, cache_key: str) -> FileArtifact | None: ...

    async def put(self, artifact: FileArtifact) -> FileArtifact: ...


class AdapterRegistryRepository(Protocol):
    """文件适配器注册表持久化能力。"""

    async def replace_all(self, adapters: list[AdapterInfo]) -> None: ...


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """文件存储适配器返回的不可变 blob 信息。"""

    sha256: str
    size_bytes: int
    storage_key: str


class FileStoragePort(Protocol):
    """原始文件内容存储端口。"""

    async def save_stream(self, chunks: AsyncIterator[bytes]) -> StoredBlob: ...

    async def cleanup_unreferenced(self, live_storage_keys: set[str]) -> int: ...

    async def discard(self, storage_key: str) -> None: ...


class FileAdapterRegistryPort(Protocol):
    """文件名/MIME 到解析适配器的选择端口。"""

    def select(self, filename: str, mime_type: str) -> AdapterInfo: ...

    def supported_extensions(self) -> list[str]: ...


class DocumentJobPort(Protocol):
    """文档处理任务入队端口。"""

    async def enqueue(self, attachment_id: str) -> bool: ...

    async def cancel(self, attachment_id: str) -> None: ...


class KnowledgeBaseRepository(Protocol):
    """知识库元数据持久化能力。"""

    async def create(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...

    async def get(self, knowledge_base_id: str) -> KnowledgeBase | None: ...

    async def list_all(self) -> list[KnowledgeBase]: ...

    async def save(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...

    async def delete(self, knowledge_base_id: str) -> bool: ...


class KnowledgeDocumentRepository(Protocol):
    """知识库文档生命周期端口。"""

    async def create(self, attachment: Attachment) -> Attachment: ...

    async def get(self, attachment_id: str, *, knowledge_base_id: str) -> Attachment | None: ...

    async def list(self, knowledge_base_id: str) -> list[Attachment]: ...

    async def list_versions(self, attachment_id: str, *, knowledge_base_id: str) -> list[Attachment]: ...

    async def latest_for_filename(self, filename: str, *, knowledge_base_id: str) -> Attachment | None: ...

    async def soft_delete(self, attachment_id: str, *, knowledge_base_id: str) -> bool: ...


class DocumentParserPort(Protocol):
    async def parse(self, attachment: Attachment) -> "ParsedDocument": ...


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """解析器输出的规范化文档。"""

    text: str
    metadata: FileMetadata = field(default_factory=FileMetadata)
    adapter_name: str | None = None
    adapter_version: str | None = None
    capabilities: tuple[str, ...] = ()


class VectorIndexerPort(Protocol):
    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None: ...

    async def delete(self, attachment_id: str) -> None: ...


class GraphIndexerPort(Protocol):
    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None: ...

    async def delete(self, attachment_id: str) -> None: ...
