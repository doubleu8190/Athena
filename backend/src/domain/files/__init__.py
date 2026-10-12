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
from .artifact_ports import AdapterRegistryRepository, FileArtifactRepository
from .indexing_ports import FileVectorSearchPort, GraphIndexerPort, VectorIndexerPort
from .processing_ports import (
    DocumentJobPort,
    DocumentJobQueue,
    DocumentParserPort,
    FileAdapterRegistryPort,
    ParsedDocument,
)
from .repository_ports import AttachmentRepository, FileChunkRepository
from .storage_ports import FileStoragePort, StoredBlob

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
    "DocumentJobQueue",
    "FileAdapterRegistryPort",
    "FileStoragePort",
    "FileLocator",
    "FileMetadata",
    "DocumentParserPort",
    "ParsedDocument",
    "VectorIndexerPort",
    "GraphIndexerPort",
    "StoredBlob",
]
