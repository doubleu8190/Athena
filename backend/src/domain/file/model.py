"""会话文件及待发布产物的显式领域类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

MAX_SESSION_FILE_BYTES = 50 * 1024 * 1024
MAX_SESSION_FILES = 100
# 会话办公文档自动提取的安全边界：单文件上限 20 MiB，提取后 UTF-8 文本上限 10 MiB。
MAX_SESSION_DOCUMENT_EXTRACT_BYTES = 20 * 1024 * 1024
MAX_SESSION_EXTRACTED_TEXT_BYTES = 10 * 1024 * 1024
EXTRACTABLE_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})

# 多模态视觉输入图片的安全边界：仅接受 PNG/JPEG/WEBP，单轮上限 5 张；
# 原始字节 <= 20 MiB、归一化后 <= 4 MiB（编码前）、长边 <= 2048px、像素 <= 40M 防解压炸弹。
VisionImageMimeType = Literal["image/png", "image/jpeg", "image/webp"]
VISION_IMAGE_MIME_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_VISION_IMAGES_PER_RUN = 5
MAX_VISION_RAW_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VISION_ENCODED_IMAGE_BYTES = 4 * 1024 * 1024
MAX_VISION_IMAGE_EDGE = 2048
MAX_VISION_IMAGE_PIXELS = 40_000_000


class SessionFileSource(StrEnum):
    """会话中已发布文件的来源。"""

    UPLOAD = "upload"
    ARTIFACT = "artifact"


class SessionFileExtractionStatus(StrEnum):
    """会话附件文本提取缓存状态。"""

    NONE = "none"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionDocumentExtractionResult:
    """PDF / Office 会话附件提取结果；失败时只暴露稳定安全错误码。"""

    status: SessionFileExtractionStatus
    text: str | None = None
    error_code: str | None = None

    @classmethod
    def ready(cls, text: str) -> SessionDocumentExtractionResult:
        """构造成功提取的结构化 UTF-8 文本结果。"""
        return cls(
            status=SessionFileExtractionStatus.READY,
            text=text,
            error_code=None,
        )

    @classmethod
    def failed(cls, error_code: str) -> SessionDocumentExtractionResult:
        """构造安全降级的提取失败结果，绝不携带原始异常或宿主路径。"""
        return cls(
            status=SessionFileExtractionStatus.FAILED,
            text=None,
            error_code=error_code,
        )


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
    extraction_status: SessionFileExtractionStatus = SessionFileExtractionStatus.NONE
    extracted_text_key: str | None = None
    extracted_text_bytes: int | None = None
    extraction_error_code: str | None = None


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
    extraction_status: SessionFileExtractionStatus = SessionFileExtractionStatus.NONE
    extracted_text_key: str | None = None
    extracted_text_bytes: int | None = None
    extraction_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SessionFilePage:
    """按稳定创建顺序排列的一页会话文件。"""

    items: tuple[SessionFile, ...]
    next_after_file_id: str | None


@dataclass(frozen=True, slots=True)
class SessionFileTextSlice:
    """严格 UTF-8 文本原件或提取文档缓存的字节偏移读取结果。"""

    file: SessionFile
    offset: int
    next_offset: int
    content: str
    eof: bool
    truncated: bool
    source_kind: Literal["raw_text", "extracted_document"] = "raw_text"


@dataclass(frozen=True, slots=True)
class FileObjectScanEntry:
    """对象存储底层扫描到的一个原始键及其回收候选判定。"""

    object_key: str
    is_candidate: bool


@dataclass(frozen=True, slots=True)
class NormalizedVisionImage:
    """完成方向校正、安全缩放与隐私元数据剥离后的视觉输入图片；base64 载荷不进入 repr。"""

    file_id: str
    filename: str
    mime_type: VisionImageMimeType
    data_base64: str = field(repr=False)
    width: int
    height: int
    size_bytes: int


class VisionImageError(ValueError):
    """视觉图片加载或归一化失败时携带稳定、可安全公开的错误码。"""

    def __init__(self, code: str) -> None:
        """保存稳定安全错误码，不暴露底层异常或宿主路径。"""
        super().__init__(code)
        self.code = code


class TextFileReadError(ValueError):
    """文本读取失败时携带稳定、可安全展示的错误码。"""

    def __init__(self, code: str, file: SessionFile | None = None) -> None:
        """保存错误码及已授权元数据。"""
        super().__init__(code)
        self.code = code
        self.file = file


__all__ = [
    "EXTRACTABLE_DOCUMENT_EXTENSIONS",
    "MAX_SESSION_DOCUMENT_EXTRACT_BYTES",
    "MAX_SESSION_EXTRACTED_TEXT_BYTES",
    "MAX_SESSION_FILE_BYTES",
    "MAX_SESSION_FILES",
    "MAX_VISION_ENCODED_IMAGE_BYTES",
    "MAX_VISION_IMAGES_PER_RUN",
    "MAX_VISION_IMAGE_EDGE",
    "MAX_VISION_IMAGE_PIXELS",
    "MAX_VISION_RAW_IMAGE_BYTES",
    "VISION_IMAGE_MIME_TYPES",
    "FileObjectScanEntry",
    "NormalizedVisionImage",
    "PendingSessionFile",
    "SessionDocumentExtractionResult",
    "SessionFile",
    "SessionFileExtractionStatus",
    "SessionFilePage",
    "SessionFileSource",
    "SessionFileTextSlice",
    "TextFileReadError",
    "VisionImageError",
    "VisionImageMimeType",
]
