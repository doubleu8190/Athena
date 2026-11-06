"""MCP Server 管理与工具目录读取用例。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from common.ids import new_id, now_utc
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.mcp import (
    DiscoveredMcpTool,
    McpAdminRequiredError,
    McpConfigVersionConflictError,
    McpConnectionInput,
    McpDiscovery,
    McpInvalidSessionError,
    McpServer,
    McpServerConfig,
    McpServerNotFoundError,
    McpServerPage,
    McpServerRepository,
    McpServerWrite,
)
from domain.tool import (
    AdminToolDefinition,
    PublicToolDefinition,
    ToolCatalogReader,
    ToolDefinition,
    ToolPage,
)

_TOOL_ID_PATTERN = re.compile(r"^tool_([0-9a-f]{32})$")


class ToolManager:
    """按当前登录身份管理 MCP 目录并协调受控发现。"""

    def __init__(
        self,
        server_repository: McpServerRepository,
        tool_reader: ToolCatalogReader,
        discovery: McpDiscovery,
        auth_repository: CurrentSessionReader,
        *,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """显式注入目录、认证、发现、ID 与时钟依赖。

        参数：server_repository 管理 Server 原子写入；tool_reader 读取安全工具投影；
        discovery 读取远端工具；auth_repository 重新验证当前 session；id_factory 生成带前缀 ID；
        clock 返回当前 UTC 时间。
        返回：可供应用层调用的用例服务。
        异常：不在构造时检查外部资源。
        """
        self._servers = server_repository
        self._tools = tool_reader
        self._discovery = discovery
        self._auth = auth_repository
        self._id_factory = id_factory
        self._clock = clock

    async def register_mcp(
        self,
        actor: AuthContext,
        name: str,
        config: McpServerConfig,
        secrets: Mapping[str, str],
    ) -> McpServer:
        """授权管理员后发现远端工具并原子注册 Server。

        参数：actor 是已认证上下文；name、config 和 secrets 是完整新 Server 配置。
        返回：已保存且不含秘密值的 Server。
        异常：session 失效、权限不足、发现失败或目录冲突时抛固定领域异常。
        """
        await self._authorize(actor, admin=True)
        server_id = self._id_factory("mcp")
        write = McpServerWrite(server_id, name, config, secrets)
        discovered = await self._discovery.discover(
            McpConnectionInput(config=write.config, secrets=write.secrets)
        )
        tools = self._map_tools(server_id, discovered)
        return await self._servers.create_with_tools(actor, write, tools)

    async def list_mcp_servers(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> McpServerPage:
        """授权管理员后分页读取 Server 安全投影。"""
        await self._authorize(actor, admin=True)
        self._validate_page(offset, limit)
        return await self._servers.list_page(offset=offset, limit=limit)

    async def get_mcp_server(self, actor: AuthContext, server_id: str) -> McpServer:
        """授权管理员后读取 Server 安全投影。"""
        await self._authorize(actor, admin=True)
        server = await self._servers.get(server_id)
        if server is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        return server

    async def update_mcp(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        name: str,
        config: McpServerConfig,
        secrets: Mapping[str, str],
    ) -> McpServer:
        """授权后读取版本、发现完整新配置，再原子提交版本比较更新。"""
        await self._authorize(actor, admin=True)
        current = await self._servers.get(server_id)
        if current is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        if current.config_version != expected_config_version:
            raise McpConfigVersionConflictError("mcp_config_version_conflict")
        replacement = McpServerWrite(server_id, name, config, secrets)
        discovered = await self._discovery.discover(
            McpConnectionInput(config=replacement.config, secrets=replacement.secrets)
        )
        tools = self._map_tools(server_id, discovered)
        return await self._servers.replace_with_tools(
            actor,
            server_id,
            expected_config_version,
            replacement,
            tools,
        )

    async def unregister_mcp(self, actor: AuthContext, server_id: str) -> None:
        """授权管理员后物理注销 Server 及其工具目录。"""
        await self._authorize(actor, admin=True)
        await self._servers.delete(actor, server_id)

    async def list_server_tools(
        self,
        actor: AuthContext,
        server_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> ToolPage[AdminToolDefinition]:
        """授权管理员后读取指定 Server 的工具来源投影。"""
        await self._authorize(actor, admin=True)
        self._validate_page(offset, limit)
        server = await self._servers.get(server_id)
        if server is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        return await self._tools.list_server_admin(server_id, offset=offset, limit=limit)

    async def list_admin_tools(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> ToolPage[AdminToolDefinition]:
        """授权管理员后读取全局工具来源投影。"""
        await self._authorize(actor, admin=True)
        self._validate_page(offset, limit)
        return await self._tools.list_admin(offset=offset, limit=limit)

    async def get_admin_tool(self, actor: AuthContext, tool_id: str) -> AdminToolDefinition:
        """授权管理员后读取单个工具的来源投影。"""
        await self._authorize(actor, admin=True)
        tool = await self._tools.get_admin_tool(tool_id)
        if tool is None:
            raise McpServerNotFoundError("mcp_tool_not_found")
        return tool

    async def list_tools(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> ToolPage[ToolDefinition]:
        """授权有效用户后读取受信内部完整工具目录。"""
        await self._authorize(actor, admin=False)
        self._validate_page(offset, limit)
        return await self._tools.list_internal(offset=offset, limit=limit)

    async def list_public_tools(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> ToolPage[PublicToolDefinition]:
        """授权有效用户后读取不含 Server 来源的工具目录。"""
        await self._authorize(actor, admin=False)
        self._validate_page(offset, limit)
        return await self._tools.list_public(offset=offset, limit=limit)

    async def get_public_tool(self, actor: AuthContext, tool_id: str) -> PublicToolDefinition:
        """授权有效用户后按 ID 读取公共工具投影。"""
        await self._authorize(actor, admin=False)
        tool = await self._tools.get_public_tool(tool_id)
        if tool is None:
            raise McpServerNotFoundError("mcp_tool_not_found")
        return tool

    async def _authorize(self, actor: AuthContext, *, admin: bool) -> None:
        """按可信 user/session ID 读取当前状态后再触碰目录资源。"""
        current = await self._auth.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if current is None:
            raise McpInvalidSessionError("mcp_session_invalid")
        if admin and current.role is not UserRole.ADMIN:
            raise McpAdminRequiredError("mcp_admin_required")

    def _map_tools(
        self, server_id: str, discovered: Sequence[DiscoveredMcpTool]
    ) -> tuple[ToolDefinition, ...]:
        """为发现结果分配彼此对应的本地 tool ID 与 model name。"""
        tools: list[ToolDefinition] = []
        for remote_tool in discovered:
            tool_id = self._id_factory("tool")
            match = _TOOL_ID_PATTERN.fullmatch(tool_id)
            if match is None:
                raise ValueError("mcp_tool_id_factory_invalid")
            uuid_hex = match.group(1)
            tools.append(
                ToolDefinition(
                    tool_id=tool_id,
                    server_id=server_id,
                    remote_name=remote_tool.remote_name,
                    model_name=f"mcp__{uuid_hex}",
                    description=remote_tool.description,
                    input_schema=remote_tool.input_schema,
                )
            )
        return tuple(tools)

    @staticmethod
    def _validate_page(offset: int, limit: int) -> None:
        """限制分页输入，避免无界读取。"""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")


__all__ = ["ToolManager"]
