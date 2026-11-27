"""会话文件 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from domain.file import SessionFileSource


class SessionFileResponse(BaseModel):
    id: str
    session_id: str
    # 复用领域 SessionFileSource 枚举，收紧为封闭文件来源集合。
    source: SessionFileSource
    filename: str
    label: str | None
    mime_type: str
    size_bytes: int
    extraction_status: Literal["none", "ready", "failed"]
    extracted_text_bytes: int | None
    extraction_error_code: str | None
    created_at: datetime


class SessionFileListResponse(BaseModel):
    items: list[SessionFileResponse]
    next_after_file_id: str | None


class SessionFileContentResponse(BaseModel):
    file: SessionFileResponse
    source_kind: Literal["raw_text", "extracted_document"]
    offset: int
    next_offset: int
    content: str
    eof: bool
    truncated: bool


__all__ = [
    "SessionFileContentResponse",
    "SessionFileListResponse",
    "SessionFileResponse",
]
