"""会话 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionCreateRequest(BaseModel):
    """创建会话请求；项目绑定与标题均可省略。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    project_id: str | None = None


class SessionUpdateRequest(BaseModel):
    """修改标题或项目绑定；显式 null 表示清空标题或解绑项目。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    project_id: str | None = None


class SessionResponse(BaseModel):
    """公开会话字段。"""

    id: str
    title: str | None
    project_id: str | None
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    """会话分页响应。"""

    items: list[SessionResponse]
    total: int
    page: int
    page_size: int


__all__ = [
    "SessionCreateRequest",
    "SessionListResponse",
    "SessionResponse",
    "SessionUpdateRequest",
]
