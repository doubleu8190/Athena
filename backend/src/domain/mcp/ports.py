"""MCP Server 目录持久化端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from domain.access import UserAccessSnapshot
from domain.auth import AuthContext
from domain.mcp.model import (
    DiscoveredMcpTool,
    McpConnectionInput,
    McpRuntimeToolTarget,
    McpServer,
    McpServerConnectionState,
    McpServerPage,
    McpServerWrite,
)
from domain.mcp.model.execution import McpCallResult
from domain.tool import (
    AdminToolDefinition,
    PublicToolDefinition,
    RuntimeToolDefinition,
    ToolDefinition,
    ToolMetadataPatch,
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

    async def patch_server(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        replacement: McpServerWrite,
        *,
        enabled: bool,
        sync_grants: bool,
        rediscovered_tools: Sequence[ToolDefinition] | None = None,
    ) -> McpServer:
        """比较配置版本后原子应用保密局部更新；仅当 rediscovered_tools 非空时增量对齐工具快照。"""

    async def set_enabled(
        self,
        actor: AuthContext,
        server_id: str,
        enabled: bool,
        *,
        expected_config_version: int | None = None,
    ) -> McpServer:
        """原子切换未软删 Server 的启停状态并递增 config_version。"""

    async def sync_discovered_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """结合 CAS 版本保护增量对齐远端发现工具，保留存活工具的身份与审批展示元信息。"""

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        """同一事务复核管理员后软删除 Server、清空密文并移除其工具与可见性授权。"""

    async def get(self, server_id: str) -> McpServer | None:
        """读取未软删 Server 的不含秘密明文投影。"""

    async def get_connection_state(self, server_id: str) -> McpServerConnectionState | None:
        """读取未软删 Server 及已解封凭据，仅用于内部保密局部更新和独立重发现。"""

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        """分页读取未软删 Server 安全投影。"""

    async def update_tool_metadata(
        self, actor: AuthContext, tool_id: str, patch: ToolMetadataPatch
    ) -> AdminToolDefinition:
        """复核管理员后局部更新工具审批开关与业务化审批展示元信息。"""

    async def batch_set_requires_approval(
        self,
        actor: AuthContext,
        tool_ids: Sequence[str],
        requires_approval: bool,
    ) -> tuple[AdminToolDefinition, ...]:
        """单事务原子批量切换全部目标工具的审批开关，任一缺失整批回滚。"""

    async def list_readable_public(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
        snapshot: UserAccessSnapshot | None = None,
    ) -> ToolPage[PublicToolDefinition]:
        """单会话按用户权限快照下推读取可读且已启用的公共工具分页。"""

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

    async def list_readable_runtime_tools(
        self,
        snapshot: UserAccessSnapshot | None,
        *,
        limit: int = 201,
    ) -> tuple[RuntimeToolDefinition, ...]:
        """按用户权限快照下推读取当前用户可读的已启用运行时 MCP 工具。"""

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
