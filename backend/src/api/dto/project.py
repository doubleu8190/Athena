"""项目 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreateRequest(BaseModel):
    """创建项目请求；兼容仅传 name，可选携带简介、背景指令与默认技能列表。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    instructions: str | None = None
    default_skill_ids: list[str] = Field(default_factory=list)


class ProjectUpdateRequest(BaseModel):
    """局部更新项目请求；显式传 null 可清空可选字段。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    default_skill_ids: list[str] | None = None


class ProjectResponse(BaseModel):
    """公开项目字段。"""

    id: str
    name: str
    description: str | None = None
    instructions: str | None = None
    default_skill_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """项目分页响应。"""

    items: list[ProjectResponse]
    total: int
    page: int
    page_size: int


__all__ = [
    "ProjectCreateRequest",
    "ProjectListResponse",
    "ProjectResponse",
    "ProjectUpdateRequest",
]
