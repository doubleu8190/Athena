"""项目 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProjectNameRequest(BaseModel):
    """创建或改名请求。"""

    model_config = ConfigDict(extra="forbid")

    name: str


class ProjectResponse(BaseModel):
    """公开项目字段。"""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """项目分页响应。"""

    items: list[ProjectResponse]
    total: int
    page: int
    page_size: int


__all__ = ["ProjectListResponse", "ProjectNameRequest", "ProjectResponse"]
