"""MCP Server 管理与工具目录读取用例。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from app.access import AccessPolicyService
from common.ids import new_id, now_utc
from domain.access import (
    AccessPermissionDeniedError,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
)
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
    ToolDefinition,
    ToolPage,
)

_TOOL_ID_PATTERN = re.compile(r"^tool_([0-9a-f]{32})$")


class ToolManager:
    """按当前登录身份管理 MCP 目录并协调受控发现。"""

    def __init__(
        self,
        repository: McpServerRepository,
        discovery: McpDiscovery,
        auth_repository: CurrentSessionReader,
        *,
        access_policy: AccessPolicyService | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """显式注入目录、认证、发现、权限引擎、ID 与时钟依赖。"""
        self._servers = repository
        self._tools = repository
        self._discovery = discovery
        self._auth = auth_repository
        self._access_policy = access_policy
        self._id_factory = id_factory
        self._clock = clock

    async def register_mcp(
        self,
        actor: AuthContext,
        name: str,
        config: McpServerConfig,
        secrets: Mapping[str, str],
        *,
        visibility_scope: VisibilityScope = VisibilityScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> McpServer:
        """授权管理员后校验三层可见性、发现远端工具并原子注册 Server。

        参数：actor 是已认证上下文；name、config、secrets 与可见性配置为完整新 Server 输入。
        返回：已保存且不含秘密值的 Server。
        异常：session 失效、权限不足、可见性配置无效、发现失败或目录冲突时抛固定领域异常。
        """
        await self._authorize(actor, admin=True)
        resolved_scope, resolved_dept, resolved_grants = await self._resolve_visibility(
            actor,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
            for_create=True,
        )
        server_id = self._id_factory("mcp")
        write = McpServerWrite(
            server_id,
            name,
            config,
            secrets,
            visibility_scope=resolved_scope,
            owner_department_id=resolved_dept,
            visibility_grants=resolved_grants,
        )
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
        *,
        visibility_scope: VisibilityScope = VisibilityScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> McpServer:
        """授权后读取版本、校验可见性、发现完整新配置，再原子提交版本比较更新。"""
        await self._authorize(actor, admin=True)
        current = await self._servers.get(server_id)
        if current is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        if current.config_version != expected_config_version:
            raise McpConfigVersionConflictError("mcp_config_version_conflict")
        resolved_scope, resolved_dept, resolved_grants = await self._resolve_visibility(
            actor,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
            for_create=False,
            existing=current,
        )
        replacement = McpServerWrite(
            server_id,
            name,
            config,
            secrets,
            visibility_scope=resolved_scope,
            owner_department_id=resolved_dept,
            visibility_grants=resolved_grants,
        )
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

    async def update_tool_approval_policy(
        self, actor: AuthContext, tool_id: str, requires_approval: bool
    ) -> AdminToolDefinition:
        """仅允许管理员更新已注册 MCP 工具的审批策略。"""
        await self._authorize(actor, admin=True)
        if not tool_id or len(tool_id) > 64 or type(requires_approval) is not bool:
            raise ValueError("mcp_tool_policy_invalid")
        return await self._servers.set_requires_approval(actor, tool_id, requires_approval)

    async def list_public_tools(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> ToolPage[PublicToolDefinition]:
        """授权有效用户后按当前用户实时可读的 MCP Server 过滤读取工具目录。"""
        await self._authorize(actor, admin=False)
        self._validate_page(offset, limit)
        if self._access_policy is not None:
            readable_server_ids = await self._access_policy.filter_readable_ids(
                actor.user.id,
                ResourceType.MCP_SERVER,
            )
            if not readable_server_ids:
                return ToolPage(items=(), total=0)
            return await self._tools.list_public(
                offset=offset,
                limit=limit,
                allowed_server_ids=tuple(readable_server_ids),
            )
        return await self._tools.list_public(offset=offset, limit=limit)

    async def get_public_tool(self, actor: AuthContext, tool_id: str) -> PublicToolDefinition:
        """授权有效用户后按 ID 读取公共工具投影；无权访问的部门工具返回 404。"""
        await self._authorize(actor, admin=False)
        if self._access_policy is not None:
            admin_tool = await self._tools.get_admin_tool(tool_id)
            if admin_tool is None:
                raise McpServerNotFoundError("mcp_tool_not_found")
            if not await self._access_policy.can_read_resource(
                actor.user.id,
                ResourceType.MCP_SERVER,
                admin_tool.server_id,
            ):
                raise McpServerNotFoundError("mcp_tool_not_found")
        tool = await self._tools.get_public_tool(tool_id)
        if tool is None:
            raise McpServerNotFoundError("mcp_tool_not_found")
        return tool

    async def _resolve_visibility(
        self,
        actor: AuthContext,
        *,
        visibility_scope: VisibilityScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
        for_create: bool,
        existing: McpServer | None = None,
    ) -> tuple[VisibilityScope, str | None, tuple[VisibilityGrantInput, ...]]:
        """校验 MCP Server 可见性配置并按需补齐默认部门授权。"""
        if self._access_policy is None:
            if visibility_scope is not VisibilityScope.COMPANY or owner_department_id is not None:
                raise ValueError("mcp_visibility_requires_access_policy")
            if visibility_grants:
                raise ValueError("mcp_visibility_grants_not_allowed")
            return VisibilityScope.COMPANY, None, ()
        existing_target = (
            None
            if existing is None
            else ResourceAccessTarget(
                resource_type=ResourceType.MCP_SERVER,
                resource_id=existing.id,
                visibility_scope=existing.visibility_scope,
                owner_id=None,
                owner_department_id=existing.owner_department_id,
            )
        )
        try:
            (
                resolved_scope,
                resolved_dept,
                resolved_grants,
            ) = await self._access_policy.resolve_and_validate_visibility(
                user_id=actor.user.id,
                resource_type=ResourceType.MCP_SERVER,
                visibility_scope=visibility_scope,
                owner_department_id=owner_department_id,
                visibility_grants=visibility_grants,
                for_create=for_create,
                existing_target=existing_target,
                preserve_existing_grants_when_none=False,
            )
        except AccessPermissionDeniedError as error:
            raise McpAdminRequiredError("mcp_admin_required") from error
        except InvalidVisibilityConfigError as error:
            raise ValueError(error.code) from error
        return resolved_scope, resolved_dept, resolved_grants or ()

    async def _authorize(self, actor: AuthContext, *, admin: bool) -> None:
        """按可信 user/session ID 读取当前状态后再触碰目录资源。"""
        current = await self._auth.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if current is None:
            raise McpInvalidSessionError("mcp_session_invalid")
        if admin and current.role is not UserRole.SUPER_ADMIN:
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
