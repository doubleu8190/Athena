"""会话文件领域契约。"""

from domain.file.model import (
    EXTRACTABLE_DOCUMENT_EXTENSIONS,
    MAX_SESSION_DOCUMENT_EXTRACT_BYTES,
    MAX_SESSION_EXTRACTED_TEXT_BYTES,
    MAX_SESSION_FILE_BYTES,
    MAX_SESSION_FILES,
    FileObjectScanEntry,
    PendingSessionFile,
    SessionDocumentExtractionResult,
    SessionFile,
    SessionFileExtractionStatus,
    SessionFilePage,
    SessionFileSource,
    SessionFileTextSlice,
    TextFileReadError,
)
from domain.file.ports import (
    FileObjectReferenceRepository,
    FileObjectStore,
    SessionDocumentExtractor,
    SessionFileRepository,
)

__all__ = [
    "EXTRACTABLE_DOCUMENT_EXTENSIONS",
    "FileObjectReferenceRepository",
    "FileObjectScanEntry",
    "FileObjectStore",
    "MAX_SESSION_DOCUMENT_EXTRACT_BYTES",
    "MAX_SESSION_EXTRACTED_TEXT_BYTES",
    "MAX_SESSION_FILE_BYTES",
    "MAX_SESSION_FILES",
    "PendingSessionFile",
    "SessionDocumentExtractionResult",
    "SessionDocumentExtractor",
    "SessionFile",
    "SessionFileExtractionStatus",
    "SessionFilePage",
    "SessionFileRepository",
    "SessionFileSource",
    "SessionFileTextSlice",
    "TextFileReadError",
]
