"""工具目录 HTTP DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicToolResponse(BaseModel):
    """普通用户可见的最小工具投影。"""

    model_config = ConfigDict(from_attributes=True)

    tool_id: str
    model_name: str
    description: str
    input_schema: dict[str, Any]


class AdminToolResponse(PublicToolResponse):
    """管理员可见的工具来源与业务审批展示元信息投影。"""

    server_id: str
    remote_name: str
    requires_approval: bool
    display_name: str | None
    approval_summary_template: str | None
    parameter_labels: dict[str, str]


class ToolApprovalPolicyRequest(BaseModel):
    """局部更新工具审批策略与业务审批展示元信息请求，向后兼容仅传 requires_approval。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    requires_approval: bool | None = None
    display_name: str | None = None
    approval_summary_template: str | None = None
    parameter_labels: dict[str, str] | None = None


class ToolBatchApprovalRequest(BaseModel):
    """原子批量切换工具审批开关的请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_ids: list[str] = Field(min_length=1, max_length=100)
    requires_approval: bool


class AdminToolBatchApprovalResponse(BaseModel):
    """批量审批开关更新结果响应。"""

    updated_count: int
    items: list[AdminToolResponse]


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
    "AdminToolBatchApprovalResponse",
    "AdminToolListResponse",
    "AdminToolResponse",
    "PublicToolListResponse",
    "PublicToolResponse",
    "ToolApprovalPolicyRequest",
    "ToolBatchApprovalRequest",
]
