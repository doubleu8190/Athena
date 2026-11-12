"""会话文件及待发布产物的显式领域类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_SESSION_FILE_BYTES = 50 * 1024 * 1024
MAX_SESSION_FILES = 100


class SessionFileSource(StrEnum):
    """会话中已发布文件的来源。"""

    UPLOAD = "upload"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class PendingSessionFile:
    """对象已写入私有存储、等待与调用结果同事务发布的文件。"""

    id: str
    filename: str
    label: str | None
    mime_type: str
    size_bytes: int
    sha256: str
    object_key: str
    output_index: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionFile:
    """已发布且仍受会话权限约束的文件元数据。"""

    id: str
    session_id: str
    owner_id: str
    source: SessionFileSource
    filename: str
    label: str | None
    mime_type: str
    size_bytes: int
    sha256: str
    object_key: str
    source_call_id: str | None
    output_index: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionFilePage:
    """按稳定创建顺序排列的一页会话文件。"""

    items: tuple[SessionFile, ...]
    next_after_file_id: str | None


@dataclass(frozen=True, slots=True)
class SessionFileTextSlice:
    """严格 UTF-8 文本文件的字节偏移读取结果。"""

    file: SessionFile
    offset: int
    next_offset: int
    content: str
    eof: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileObjectScanEntry:
    """对象存储底层扫描到的一个原始键及其回收候选判定。"""

    object_key: str
    is_candidate: bool


class TextFileReadError(ValueError):
    """文本读取失败时携带稳定、可安全展示的错误码。"""

    def __init__(self, code: str, file: SessionFile | None = None) -> None:
        """保存错误码及已授权元数据。"""
        super().__init__(code)
        self.code = code
        self.file = file


__all__ = [
    "MAX_SESSION_FILE_BYTES",
    "MAX_SESSION_FILES",
    "FileObjectScanEntry",
    "PendingSessionFile",
    "SessionFile",
    "SessionFilePage",
    "SessionFileSource",
    "SessionFileTextSlice",
    "TextFileReadError",
]
