"""文件和附件领域。"""

from .entities import (
    AdapterInfo,
    Attachment,
    AttachmentRef,
    AttachmentStatus,
    FileChunk,
    FileArtifact,
    FileLocator,
    FileMetadata,
)
from .ports import (
    AdapterRegistryRepository,
    AttachmentRepository,
    FileArtifactRepository,
    FileChunkRepository,
    FileVectorSearchPort,
    DocumentParserPort,
    ParsedDocument,
    VectorIndexerPort,
    GraphIndexerPort,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileStoragePort,
    StoredBlob,
)

# 迁移期兼容导出。知识库业务代码应从 ``domain.knowledge`` 导入。
from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository, KnowledgeDocumentRepository

__all__ = [
    "Attachment",
    "AttachmentRef",
    "AdapterInfo",
    "AdapterRegistryRepository",
    "AttachmentRepository",
    "AttachmentStatus",
    "FileChunk",
    "FileArtifact",
    "FileArtifactRepository",
    "FileChunkRepository",
    "FileVectorSearchPort",
    "DocumentJobPort",
    "FileAdapterRegistryPort",
    "FileStoragePort",
    "FileLocator",
    "FileMetadata",
    "KnowledgeBase",
    "KnowledgeBaseRepository",
    "KnowledgeDocumentRepository",
    "DocumentParserPort",
    "ParsedDocument",
    "VectorIndexerPort",
    "GraphIndexerPort",
    "StoredBlob",
]
