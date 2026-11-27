"""个人工作偏好与显式记忆 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class UpdateUserWorkProfileRequest(BaseModel):
    """更新个人工作偏好与显式记忆开关请求。"""

    model_config = ConfigDict(extra="forbid")

    custom_instructions: str
    memory_enabled: bool


class UserWorkProfileResponse(BaseModel):
    """个人工作偏好响应。"""

    model_config = ConfigDict(from_attributes=True)

    custom_instructions: str
    memory_enabled: bool
    updated_at: datetime | None


class CreateUserMemoryRequest(BaseModel):
    """手动新增显式记忆请求。"""

    model_config = ConfigDict(extra="forbid")

    content: str


class UpdateUserMemoryRequest(BaseModel):
    """修改显式记忆正文请求。"""

    model_config = ConfigDict(extra="forbid")

    content: str


class UserMemoryResponse(BaseModel):
    """单条显式记忆响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    content: str
    source: Literal["manual", "chat_explicit"]
    created_at: datetime
    updated_at: datetime


class UserMemoryListResponse(BaseModel):
    """当前用户全部显式记忆列表及配额响应。"""

    model_config = ConfigDict(from_attributes=True)

    items: list[UserMemoryResponse]
    total: int
    limit: int


__all__ = [
    "CreateUserMemoryRequest",
    "UpdateUserMemoryRequest",
    "UpdateUserWorkProfileRequest",
    "UserMemoryListResponse",
    "UserMemoryResponse",
    "UserWorkProfileResponse",
]
