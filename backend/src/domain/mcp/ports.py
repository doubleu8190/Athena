"""MCP Server 目录持久化端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from domain.auth import AuthContext
from domain.mcp.model import (
    DiscoveredMcpTool,
    McpConnectionInput,
    McpRuntimeToolTarget,
    McpServer,
    McpServerPage,
    McpServerWrite,
)
from domain.mcp.model.execution import McpCallResult
from domain.tool import (
    AdminToolDefinition,
    PublicToolDefinition,
    RuntimeToolDefinition,
    ToolDefinition,
    ToolPage,
)


class McpServerRepository(Protocol):
    """管理 MCP Server、工具目录投影与发现快照的统一持久化端口。"""

    async def create_with_tools(
        self,
        actor: AuthContext,
        server: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """同一事务复核管理员后创建 Server 及全部工具。"""

    async def replace_with_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        replacement: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """比较配置版本后原子替换 Server 配置和工具快照。"""

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        """同一事务复核管理员后物理删除 Server 及其工具。"""

    async def get(self, server_id: str) -> McpServer | None:
        """读取不含秘密明文的 Server 投影。"""

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        """分页读取 Server 安全投影。"""

    async def set_requires_approval(
        self, actor: AuthContext, tool_id: str, requires_approval: bool
    ) -> AdminToolDefinition:
        """复核管理员后更新已注册工具的审批策略。"""

    async def list_public(
        self,
        *,
        offset: int,
        limit: int,
        allowed_server_ids: Sequence[str] | None = None,
    ) -> ToolPage[PublicToolDefinition]:
        """读取供普通调用方使用的工具定义，支持按有权限的 MCP Server ID 集合过滤。"""

    async def list_admin(self, *, offset: int, limit: int) -> ToolPage[AdminToolDefinition]:
        """读取包含 Server 来源映射的管理员工具定义。"""

    async def list_server_admin(
        self, server_id: str, *, offset: int, limit: int
    ) -> ToolPage[AdminToolDefinition]:
        """读取指定 Server 的管理员工具定义。"""

    async def get_public_tool(self, tool_id: str) -> PublicToolDefinition | None:
        """按 ID 读取普通调用方安全投影。"""

    async def get_admin_tool(self, tool_id: str) -> AdminToolDefinition | None:
        """按 ID 读取管理员安全投影。"""


class McpDiscovery(Protocol):
    """通过受控传输读取远端工具目录。"""

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        """建立短时连接并完整读取工具分页，结束前确认资源已清理。"""


class McpToolRuntimeReader(Protocol):
    """Read one current routing target without exposing it to API callers."""

    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
        """Read at most ``limit`` catalog rows with one config version per Server."""

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        """Return the target only when its DB config version still matches."""


class McpToolExecutor(Protocol):
    """Execute exactly one tools/call over a fresh connection."""

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        """Connect, list for header state, call once, and always close resources."""


__all__ = [
    "McpDiscovery",
    "McpServerRepository",
    "McpToolExecutor",
    "McpToolRuntimeReader",
]
