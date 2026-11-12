"""会话文件领域对象到 HTTP DTO 的纯转换。"""

from __future__ import annotations

from api.dto.file import SessionFileListResponse, SessionFileResponse
from domain.file import SessionFile, SessionFilePage


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
        created_at=file.created_at,
    )


def session_file_page_to_response(page: SessionFilePage) -> SessionFileListResponse:
    """转换稳定游标分页。"""
    return SessionFileListResponse(
        items=[session_file_to_response(item) for item in page.items],
        next_after_file_id=page.next_after_file_id,
    )


__all__ = ["session_file_page_to_response", "session_file_to_response"]
