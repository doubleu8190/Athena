"""工具目录 HTTP DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PublicToolResponse(BaseModel):
    """普通用户可见的最小工具投影。"""

    tool_id: str
    model_name: str
    description: str
    input_schema: dict[str, Any]


class AdminToolResponse(PublicToolResponse):
    """管理员可见的工具来源投影。"""

    server_id: str
    remote_name: str


class PublicToolListResponse(BaseModel):
    """工具分页响应。"""

    items: list[PublicToolResponse]
    total: int
    page: int
    page_size: int


class AdminToolListResponse(BaseModel):
    """管理员工具分页响应。"""

    items: list[AdminToolResponse]
    total: int
    page: int
    page_size: int


__all__ = [
    "AdminToolListResponse",
    "AdminToolResponse",
    "PublicToolListResponse",
    "PublicToolResponse",
]
