"""文件端口的兼容导出。

正式定义按持久化、处理、存储和索引职责分布在相邻模块；保留此模块
是为了兼容现有适配器和外部调用方的导入路径。
"""

from .artifact_ports import AdapterRegistryRepository, FileArtifactRepository
from .indexing_ports import FileVectorSearchPort, GraphIndexerPort, VectorIndexerPort
from .processing_ports import DocumentJobPort, DocumentParserPort, FileAdapterRegistryPort, ParsedDocument
from .repository_ports import AttachmentRepository, FileChunkRepository
from .storage_ports import FileStoragePort, StoredBlob

__all__ = [
    "AdapterRegistryRepository",
    "AttachmentRepository",
    "DocumentJobPort",
    "DocumentParserPort",
    "FileAdapterRegistryPort",
    "FileArtifactRepository",
    "FileChunkRepository",
    "FileStoragePort",
    "FileVectorSearchPort",
    "GraphIndexerPort",
    "ParsedDocument",
    "StoredBlob",
    "VectorIndexerPort",
]
