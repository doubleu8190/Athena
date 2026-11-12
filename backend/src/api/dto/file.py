"""会话文件 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SessionFileResponse(BaseModel):
    id: str
    session_id: str
    source: str
    filename: str
    label: str | None
    mime_type: str
    size_bytes: int
    created_at: datetime


class SessionFileListResponse(BaseModel):
    items: list[SessionFileResponse]
    next_after_file_id: str | None


__all__ = ["SessionFileListResponse", "SessionFileResponse"]
