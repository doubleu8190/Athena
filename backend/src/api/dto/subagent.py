"""管理员 Subagent 配置管理 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SubagentResponse(BaseModel):
    """Subagent 配置响应投影。"""

    id: str
    slug: str
    name: str
    description: str
    system_prompt: str
    model_profile_id: str | None
    allowed_tool_ids: list[str]
    enabled: bool
    max_llm_requests: int | None
    timeout_seconds: int | None
    version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class SubagentListResponse(BaseModel):
    """Subagent 分页列表响应。"""

    items: list[SubagentResponse]
    total: int
    offset: int
    limit: int


class SubagentCreateRequest(BaseModel):
    """创建 Subagent 配置请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    slug: str
    name: str
    description: str
    system_prompt: str
    model_profile_id: str | None = None
    allowed_tool_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    max_llm_requests: int | None = None
    timeout_seconds: int | None = None


class SubagentUpdateRequest(BaseModel):
    """部分更新 Subagent 配置请求体（含乐观锁版本号）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    slug: str | None = None
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model_profile_id: str | None = None
    allowed_tool_ids: list[str] | None = None
    max_llm_requests: int | None = None
    timeout_seconds: int | None = None


class SubagentEnabledRequest(BaseModel):
    """切换 Subagent 启停状态请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class AvailableToolItemResponse(BaseModel):
    """可选工具条目响应。"""

    tool_id: str
    name: str
    description: str
    source_type: str
    requires_approval: bool
    server_id: str | None = None
    server_name: str | None = None


class AvailableToolGroupResponse(BaseModel):
    """按来源分组的可选工具目录响应。"""

    group_key: str
    label: str
    items: list[AvailableToolItemResponse]


class SubagentAvailableToolsResponse(BaseModel):
    """Subagent 可选工具目录聚合响应。"""

    groups: list[AvailableToolGroupResponse]


__all__ = [
    "AvailableToolGroupResponse",
    "AvailableToolItemResponse",
    "SubagentAvailableToolsResponse",
    "SubagentCreateRequest",
    "SubagentEnabledRequest",
    "SubagentListResponse",
    "SubagentResponse",
    "SubagentUpdateRequest",
]
