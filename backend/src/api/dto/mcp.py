"""MCP 管理接口请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.dto.access import VisibilityGrantRequest, VisibilityGrantResponse
from domain.mcp import StdioConfig, StreamableHttpConfig


class _HttpServerBase(BaseModel):
    """Streamable HTTP 类型 MCP Server 配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    transport: Literal["streamable_http"]
    url: str
    secrets: dict[str, str] = Field(default_factory=dict)
    visibility_scope: Literal["company", "department"] = "company"
    owner_department_id: str | None = None
    visibility_grants: list[VisibilityGrantRequest] | None = None

    def to_domain_config(self) -> StreamableHttpConfig:
        """转换为领域 Streamable HTTP 连接配置。"""
        return StreamableHttpConfig(transport="streamable_http", url=self.url)


class _StdioServerBase(BaseModel):
    """Stdio 子进程类型 MCP Server 配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    transport: Literal["stdio"]
    command: str
    args: list[str]
    cwd: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)
    visibility_scope: Literal["company", "department"] = "company"
    owner_department_id: str | None = None
    visibility_grants: list[VisibilityGrantRequest] | None = None

    def to_domain_config(self) -> StdioConfig:
        """转换为领域 Stdio 连接配置。"""
        return StdioConfig(
            transport="stdio",
            command=self.command,
            args=tuple(self.args),
            cwd=self.cwd,
        )


class HttpServerCreateRequest(_HttpServerBase):
    """创建 Streamable HTTP MCP Server 的请求。"""


class StdioServerCreateRequest(_StdioServerBase):
    """创建 Stdio MCP Server 的请求。"""


class HttpServerUpdateRequest(_HttpServerBase):
    """替换 Streamable HTTP MCP Server 的请求。"""

    secrets: dict[str, str]
    expected_config_version: int = Field(ge=1)


class StdioServerUpdateRequest(_StdioServerBase):
    """替换 Stdio MCP Server 的请求。"""

    secrets: dict[str, str]
    expected_config_version: int = Field(ge=1)


McpServerCreateRequest = Annotated[
    HttpServerCreateRequest | StdioServerCreateRequest,
    Field(discriminator="transport"),
]
McpServerUpdateRequest = Annotated[
    HttpServerUpdateRequest | StdioServerUpdateRequest,
    Field(discriminator="transport"),
]


class McpServerPatchRequest(BaseModel):
    """保留既有加密凭据的 MCP Server 局部更新请求。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_config_version: int = Field(ge=1)
    name: str | None = None
    config: dict[str, Any] | None = None
    secrets_upsert: dict[str, str] | None = Field(default=None, repr=False)
    secrets_remove: list[str] | None = None
    visibility_scope: Literal["company", "department"] | None = None
    owner_department_id: str | None = None
    visibility_grants: list[VisibilityGrantRequest] | None = None
    enabled: bool | None = None
    rediscover: bool = False


class McpServerEnabledRequest(BaseModel):
    """切换 MCP Server 启停状态的请求。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    expected_config_version: int | None = Field(default=None, ge=1)


class McpServerResponse(BaseModel):
    """管理员可见的 Server 配置、启停状态、可见范围与脱敏秘密状态。"""

    id: str
    name: str
    transport: str
    config: dict[str, Any]
    secrets: list[dict[str, Any]]
    config_version: int
    enabled: bool = True
    visibility_scope: Literal["company", "department"] = "company"
    owner_department_id: str | None = None
    visibility_grants: list[VisibilityGrantResponse] = Field(default_factory=list)
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


__all__ = [
    "HttpServerCreateRequest",
    "HttpServerUpdateRequest",
    "McpServerCreateRequest",
    "McpServerEnabledRequest",
    "McpServerListResponse",
    "McpServerPatchRequest",
    "McpServerResponse",
    "McpServerUpdateRequest",
    "StdioServerCreateRequest",
    "StdioServerUpdateRequest",
]
