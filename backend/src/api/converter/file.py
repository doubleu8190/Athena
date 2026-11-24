"""会话文件领域对象到 HTTP DTO 的纯转换。"""

from __future__ import annotations

from typing import Literal

from api.dto.file import (
    SessionFileContentResponse,
    SessionFileListResponse,
    SessionFileResponse,
)
from domain.file import (
    SessionFile,
    SessionFileExtractionStatus,
    SessionFilePage,
    SessionFileTextSlice,
)

_EXTRACTION_STATUS_LITERAL: dict[
    SessionFileExtractionStatus,
    Literal["none", "ready", "failed"],
] = {
    SessionFileExtractionStatus.NONE: "none",
    SessionFileExtractionStatus.READY: "ready",
    SessionFileExtractionStatus.FAILED: "failed",
}


def session_file_to_response(file: SessionFile) -> SessionFileResponse:
    """只投影授权文件元数据，不返回对象键或摘要。"""
    return SessionFileResponse(
        id=file.id,
        session_id=file.session_id,
        source=file.source.value,
        filename=file.filename,
        label=file.label,
        mime_type=file.mime_type,
        size_bytes=file.size_bytes,
        extraction_status=_EXTRACTION_STATUS_LITERAL[file.extraction_status],
        extracted_text_bytes=file.extracted_text_bytes,
        extraction_error_code=file.extraction_error_code,
        created_at=file.created_at,
    )


def session_file_page_to_response(page: SessionFilePage) -> SessionFileListResponse:
    """转换稳定游标分页。"""
    return SessionFileListResponse(
        items=[session_file_to_response(item) for item in page.items],
        next_after_file_id=page.next_after_file_id,
    )


def session_file_text_slice_to_response(
    text_slice: SessionFileTextSlice,
) -> SessionFileContentResponse:
    """转换会话文件分页文本预览响应。"""
    return SessionFileContentResponse(
        file=session_file_to_response(text_slice.file),
        source_kind=text_slice.source_kind,
        offset=text_slice.offset,
        next_offset=text_slice.next_offset,
        content=text_slice.content,
        eof=text_slice.eof,
        truncated=text_slice.truncated,
    )


__all__ = [
    "session_file_page_to_response",
    "session_file_text_slice_to_response",
    "session_file_to_response",
]
