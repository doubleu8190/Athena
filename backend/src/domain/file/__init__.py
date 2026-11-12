"""会话文件领域契约。"""

from domain.file.model import (
    MAX_SESSION_FILE_BYTES,
    MAX_SESSION_FILES,
    FileObjectScanEntry,
    PendingSessionFile,
    SessionFile,
    SessionFilePage,
    SessionFileSource,
    SessionFileTextSlice,
    TextFileReadError,
)
from domain.file.ports import (
    FileObjectReferenceRepository,
    FileObjectStore,
    SessionFileRepository,
)

__all__ = [
    "FileObjectStore",
    "FileObjectReferenceRepository",
    "MAX_SESSION_FILE_BYTES",
    "MAX_SESSION_FILES",
    "FileObjectScanEntry",
    "PendingSessionFile",
    "SessionFile",
    "SessionFilePage",
    "SessionFileSource",
    "SessionFileTextSlice",
    "SessionFileRepository",
    "TextFileReadError",
]
