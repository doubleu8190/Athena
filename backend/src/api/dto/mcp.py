"""MCP 管理接口请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _McpServerConfigurationRequest(BaseModel):
    """MCP Server 的严格 transport 配置字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    transport: Literal["streamable_http", "stdio"]
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_foreign_transport_fields(cls, value: object) -> object:
        """按输入键是否出现校验 transport 联合，包括显式 null。"""
        if not isinstance(value, dict):
            return value
        transport = value.get("transport")
        if transport == "streamable_http" and any(
            key in value for key in ("command", "args", "cwd")
        ):
            raise ValueError("mcp_config_invalid")
        if transport == "stdio" and "url" in value:
            raise ValueError("mcp_config_invalid")
        if transport == "stdio" and ("args" not in value or value.get("args") is None):
            raise ValueError("mcp_config_invalid")
        return value


class McpServerCreateRequest(_McpServerConfigurationRequest):
    """创建 MCP Server 的请求。"""


class McpServerUpdateRequest(_McpServerConfigurationRequest):
    """完整替换 MCP Server 的请求。"""

    secrets: dict[str, str]
    expected_config_version: int = Field(ge=1)


class McpServerResponse(BaseModel):
    """管理员可见的 Server 配置与脱敏秘密状态。"""

    id: str
    name: str
    transport: str
    config: dict[str, Any]
    secrets: list[dict[str, Any]]
    config_version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    tools_discovered_at: datetime | None


class McpServerListResponse(BaseModel):
    """MCP Server 分页。"""

    items: list[McpServerResponse]
    total: int
    page: int
    page_size: int


McpServerRequest = McpServerCreateRequest

__all__ = [
    "McpServerCreateRequest",
    "McpServerListResponse",
    "McpServerRequest",
    "McpServerResponse",
    "McpServerUpdateRequest",
]
