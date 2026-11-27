"""会话文件领域对象到 HTTP DTO 的纯转换。"""

from __future__ import annotations

from api.dto.file import (
    SessionFileContentResponse,
    SessionFileListResponse,
    SessionFileResponse,
)
from domain.file import SessionFile, SessionFilePage, SessionFileTextSlice


def session_file_to_response(file: SessionFile) -> SessionFileResponse:
    """只投影授权文件元数据，自动忽略 owner_id、object_key 与 sha256 等内部字段。"""
    return SessionFileResponse.model_validate(file)


def session_file_page_to_response(page: SessionFilePage) -> SessionFileListResponse:
    """转换稳定游标分页。"""
    return SessionFileListResponse.model_validate(page)


def session_file_text_slice_to_response(
    text_slice: SessionFileTextSlice,
) -> SessionFileContentResponse:
    """转换会话文件分页文本预览响应。"""
    return SessionFileContentResponse.model_validate(text_slice)


__all__ = [
    "session_file_page_to_response",
    "session_file_text_slice_to_response",
    "session_file_to_response",
]
