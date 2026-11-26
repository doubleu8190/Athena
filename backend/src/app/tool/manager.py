"""MCP Server 管理与工具目录读取用例。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

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
    StdioConfig,
    StreamableHttpConfig,
    validate_mcp_secret_placeholders,
)
from domain.tool import (
    TOOL_FIELD_UNSET,
    AdminToolDefinition,
    PublicToolDefinition,
    ToolDefinition,
    ToolMetadataPatch,
    ToolPage,
    validate_approval_summary_template,
    validate_parameter_labels,
    validate_tool_display_name,
)

_TOOL_ID_PATTERN = re.compile(r"^tool_([0-9a-f]{32})$")


def _merge_server_config(
    existing: McpServerConfig,
    patch: Mapping[str, Any],
) -> McpServerConfig:
    """按当前 Server transport 局部合并连接配置，禁止隐式切换 transport 或传入未知字段。"""
    if not isinstance(patch, Mapping) or not patch:
        raise ValueError("mcp_config_invalid")
    if isinstance(existing, StreamableHttpConfig):
        keys = set(patch)
        if not keys <= {"transport", "url"} or "url" not in patch:
            raise ValueError("mcp_config_invalid")
        if "transport" in patch and patch["transport"] != "streamable_http":
            raise ValueError("mcp_config_invalid")
        url = patch["url"]
        if not isinstance(url, str):
            raise ValueError("mcp_config_invalid")
        return StreamableHttpConfig(transport="streamable_http", url=url)
    if isinstance(existing, StdioConfig):
        keys = set(patch)
        if not keys <= {"transport", "command", "args", "cwd"}:
            raise ValueError("mcp_config_invalid")
        if not keys & {"command", "args", "cwd"}:
            raise ValueError("mcp_config_invalid")
        if "transport" in patch and patch["transport"] != "stdio":
            raise ValueError("mcp_config_invalid")
        command = existing.command
        if "command" in patch:
            if not isinstance(patch["command"], str):
                raise ValueError("mcp_config_invalid")
            command = patch["command"]
        args: tuple[str, ...] = existing.args
        if "args" in patch:
            raw_args = patch["args"]
            if not isinstance(raw_args, (list, tuple)) or any(
                not isinstance(item, str) for item in raw_args
            ):
                raise ValueError("mcp_config_invalid")
            args = tuple(raw_args)
        cwd = existing.cwd
        if "cwd" in patch:
            raw_cwd = patch["cwd"]
            if raw_cwd is not None and not isinstance(raw_cwd, str):
                raise ValueError("mcp_config_invalid")
            cwd = raw_cwd
        return StdioConfig(transport="stdio", command=command, args=args, cwd=cwd)
    raise ValueError("mcp_config_invalid")


def _merge_secrets(
    existing_secrets: Mapping[str, str],
    *,
    transport: str,
    secrets_upsert: Mapping[str, str] | None,
    secrets_remove: Sequence[str] | None,
) -> dict[str, str]:
    """保密合并新旧凭据：拒绝交集键，精确增删改，未提及旧密钥完整保留。"""
    upsert_map: dict[str, str] = {}
    if secrets_upsert is not None:
        if not isinstance(secrets_upsert, Mapping):
            raise ValueError("mcp_secret_invalid")
        for key, value in secrets_upsert.items():
            if not isinstance(key, str) or not isinstance(value, str) or not key:
                raise ValueError("mcp_secret_invalid")
            upsert_map[key] = value
    remove_keys: list[str] = []
    if secrets_remove is not None:
        if isinstance(secrets_remove, (str, bytes)) or not isinstance(secrets_remove, Sequence):
            raise ValueError("mcp_secret_invalid")
        for key in secrets_remove:
            if not isinstance(key, str) or not key or "\x00" in key:
                raise ValueError("mcp_secret_invalid")
            remove_keys.append(key)

    # 拒绝 upsert 与 remove 存在交集键（HTTP Header 额外做大小写不敏感交集检查）。
    if set(upsert_map) & set(remove_keys):
        raise ValueError("mcp_secret_overlap")
    if transport == "streamable_http" and (
        {key.lower() for key in upsert_map} & {key.lower() for key in remove_keys}
    ):
        raise ValueError("mcp_secret_overlap")

    merged = dict(existing_secrets)
    if transport == "streamable_http":
        remove_lower = {key.lower() for key in remove_keys}
        upsert_lower = {key.lower() for key in upsert_map}
        for old_key in list(merged):
            if old_key in remove_keys or old_key.lower() in remove_lower:
                merged.pop(old_key, None)
            elif old_key.lower() in upsert_lower:
                # 覆盖不同大小写写法的同一 HTTP Header，避免产生大小写重复键。
                merged.pop(old_key, None)
    else:
        for key in remove_keys:
            merged.pop(key, None)
    merged.update(upsert_map)
    return merged


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
        """授权管理员后校验三层可见性、发现远端工具并原子注册 Server。"""
        await self._authorize(actor, admin=True)
        resolved_scope, resolved_dept, resolved_grants = await self._resolve_visibility(
            actor,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
            for_create=True,
        )
        server_id = self._id_factory("mcp")
        validate_mcp_secret_placeholders(config, secrets)
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
        """授权管理员后分页读取未软删 Server 安全投影。"""
        await self._authorize(actor, admin=True)
        self._validate_page(offset, limit)
        return await self._servers.list_page(offset=offset, limit=limit)

    async def get_mcp_server(self, actor: AuthContext, server_id: str) -> McpServer:
        """授权管理员后读取未软删 Server 安全投影。"""
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
        validate_mcp_secret_placeholders(config, secrets)
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

    async def patch_mcp(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        *,
        name: str | None = None,
        config_patch: Mapping[str, Any] | None = None,
        secrets_upsert: Mapping[str, str] | None = None,
        secrets_remove: Sequence[str] | None = None,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        owner_department_id_set: bool = False,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
        visibility_grants_set: bool = False,
        enabled: bool | None = None,
        rediscover: bool = False,
    ) -> McpServer:
        """保留旧密钥的局部更新；可选在事务外使用合并凭据重发现工具并 CAS 落库。"""
        await self._authorize(actor, admin=True)
        if type(expected_config_version) is not int or expected_config_version < 1:
            raise ValueError("mcp_config_invalid")
        if type(rediscover) is not bool:
            raise ValueError("mcp_config_invalid")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("mcp_config_invalid")
        has_mutation = any(
            (
                name is not None,
                config_patch is not None,
                secrets_upsert is not None,
                secrets_remove is not None,
                visibility_scope is not None,
                owner_department_id_set,
                visibility_grants_set,
                enabled is not None,
                rediscover,
            )
        )
        if not has_mutation:
            raise ValueError("mcp_config_invalid")

        # 先在事务外读取当前配置及已解封凭据，用于保密合并与可选远端发现（不在发现期间持 DB 锁）。
        state = await self._servers.get_connection_state(server_id)
        if state is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        current = state.server
        if current.config_version != expected_config_version:
            raise McpConfigVersionConflictError("mcp_config_version_conflict")

        final_name = current.name if name is None else name
        final_config = (
            current.config
            if config_patch is None
            else _merge_server_config(current.config, config_patch)
        )
        merged_secrets = _merge_secrets(
            state.secrets,
            transport=final_config.transport,
            secrets_upsert=secrets_upsert,
            secrets_remove=secrets_remove,
        )
        validate_mcp_secret_placeholders(final_config, merged_secrets)

        has_visibility_change = (
            visibility_scope is not None or owner_department_id_set or visibility_grants_set
        )
        if has_visibility_change:
            target_scope = (
                visibility_scope if visibility_scope is not None else current.visibility_scope
            )
            if owner_department_id_set:
                target_dept = owner_department_id
            elif target_scope is VisibilityScope.DEPARTMENT:
                target_dept = current.owner_department_id
            else:
                target_dept = None
            target_grants = visibility_grants if visibility_grants_set else None
            (
                resolved_scope,
                resolved_dept,
                maybe_grants,
            ) = await self._resolve_visibility_with_preserve(
                actor,
                visibility_scope=target_scope,
                owner_department_id=target_dept,
                visibility_grants=target_grants,
                existing=current,
                preserve_existing_grants_when_none=(not visibility_grants_set),
            )
            sync_grants = maybe_grants is not None
            write_grants = () if maybe_grants is None else maybe_grants
        else:
            resolved_scope = current.visibility_scope
            resolved_dept = current.owner_department_id
            sync_grants = False
            write_grants = ()

        replacement = McpServerWrite(
            server_id,
            final_name,
            final_config,
            merged_secrets,
            visibility_scope=resolved_scope,
            owner_department_id=resolved_dept,
            visibility_grants=write_grants,
        )
        final_enabled = current.enabled if enabled is None else enabled
        rediscovered_tools: tuple[ToolDefinition, ...] | None = None
        if rediscover:
            discovered = await self._discovery.discover(
                McpConnectionInput(config=replacement.config, secrets=replacement.secrets)
            )
            rediscovered_tools = self._map_tools(server_id, discovered)

        return await self._servers.patch_server(
            actor,
            server_id,
            expected_config_version,
            replacement,
            enabled=final_enabled,
            sync_grants=sync_grants,
            rediscovered_tools=rediscovered_tools,
        )

    async def set_mcp_enabled(
        self,
        actor: AuthContext,
        server_id: str,
        enabled: bool,
        *,
        expected_config_version: int | None = None,
    ) -> McpServer:
        """原子切换 MCP Server 启停状态；停用后工具立即从运行态与公共目录摘除。"""
        await self._authorize(actor, admin=True)
        if type(enabled) is not bool:
            raise ValueError("mcp_config_invalid")
        if expected_config_version is not None and (
            type(expected_config_version) is not int or expected_config_version < 1
        ):
            raise ValueError("mcp_config_invalid")
        return await self._servers.set_enabled(
            actor,
            server_id,
            enabled,
            expected_config_version=expected_config_version,
        )

    async def rediscover_mcp(self, actor: AuthContext, server_id: str) -> McpServer:
        """读取已存凭据独立重发现远端工具，并在事务内做 CAS 保护与增量对齐。"""
        await self._authorize(actor, admin=True)
        state = await self._servers.get_connection_state(server_id)
        if state is None:
            raise McpServerNotFoundError("mcp_server_not_found")
        read_version = state.server.config_version
        discovered = await self._discovery.discover(
            McpConnectionInput(config=state.server.config, secrets=state.secrets)
        )
        tools = self._map_tools(server_id, discovered)
        return await self._servers.sync_discovered_tools(
            actor,
            server_id,
            read_version,
            tools,
        )

    async def unregister_mcp(self, actor: AuthContext, server_id: str) -> None:
        """授权管理员后软删除 Server、清空密文并移除工具和可见性授权。"""
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
        """授权管理员后读取指定未软删 Server 的工具来源投影。"""
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

    async def update_tool_metadata(
        self, actor: AuthContext, tool_id: str, patch: ToolMetadataPatch
    ) -> AdminToolDefinition:
        """仅允许管理员局部更新工具审批开关、中文展示名、审批摘要模板和参数标签映射。"""
        await self._authorize(actor, admin=True)
        if not tool_id or len(tool_id) > 64 or not patch.has_updates():
            raise ValueError("mcp_tool_policy_invalid")
        if patch.requires_approval is not TOOL_FIELD_UNSET and (
            type(patch.requires_approval) is not bool
        ):
            raise ValueError("mcp_tool_policy_invalid")
        if patch.display_name is not TOOL_FIELD_UNSET:
            validate_tool_display_name(patch.display_name)
        if patch.approval_summary_template is not TOOL_FIELD_UNSET:
            validate_approval_summary_template(patch.approval_summary_template)
        if patch.parameter_labels is not TOOL_FIELD_UNSET:
            validate_parameter_labels(patch.parameter_labels)
        return await self._servers.update_tool_metadata(actor, tool_id, patch)

    async def batch_update_tool_approval(
        self,
        actor: AuthContext,
        tool_ids: Sequence[str],
        requires_approval: bool,
    ) -> tuple[AdminToolDefinition, ...]:
        """仅允许管理员原子批量切换 1..100 个 MCP 工具的审批开关，任一缺失整批回滚。"""
        await self._authorize(actor, admin=True)
        if (
            isinstance(tool_ids, (str, bytes))
            or not isinstance(tool_ids, Sequence)
            or type(requires_approval) is not bool
        ):
            raise ValueError("mcp_tool_policy_invalid")
        ordered_ids = tuple(tool_ids)
        if (
            not 1 <= len(ordered_ids) <= 100
            or len(set(ordered_ids)) != len(ordered_ids)
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 64
                or any(unicodedata.category(ch) == "Cc" for ch in item)
                for item in ordered_ids
            )
        ):
            raise ValueError("mcp_tool_policy_invalid")
        return await self._servers.batch_set_requires_approval(
            actor, ordered_ids, requires_approval
        )

    async def list_public_tools(
        self, actor: AuthContext, *, offset: int = 0, limit: int = 20
    ) -> ToolPage[PublicToolDefinition]:
        """授权有效用户后按当前用户实时可读且已启用的 MCP Server 过滤读取工具目录。"""
        await self._authorize(actor, admin=False)
        self._validate_page(offset, limit)
        if self._access_policy is not None:
            return await self._tools.list_readable_public(
                actor.user.id,
                offset=offset,
                limit=limit,
            )
        return await self._tools.list_public(offset=offset, limit=limit)

    async def get_public_tool(self, actor: AuthContext, tool_id: str) -> PublicToolDefinition:
        """授权有效用户后按 ID 读取公共工具投影；停用或无权访问返回 404。"""
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
        scope, dept, grants = await self._resolve_visibility_with_preserve(
            actor,
            visibility_scope=visibility_scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
            existing=existing,
            for_create=for_create,
            preserve_existing_grants_when_none=False,
        )
        return scope, dept, grants or ()

    async def _resolve_visibility_with_preserve(
        self,
        actor: AuthContext,
        *,
        visibility_scope: VisibilityScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
        existing: McpServer | None = None,
        for_create: bool = False,
        preserve_existing_grants_when_none: bool = True,
    ) -> tuple[VisibilityScope, str | None, tuple[VisibilityGrantInput, ...] | None]:
        """校验可见性配置；当局部更新未传 grants 且部门未变时返回 None 保留原授权。"""
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
            return await self._access_policy.resolve_and_validate_visibility(
                user_id=actor.user.id,
                resource_type=ResourceType.MCP_SERVER,
                visibility_scope=visibility_scope,
                owner_department_id=owner_department_id,
                visibility_grants=visibility_grants,
                for_create=for_create,
                existing_target=existing_target,
                preserve_existing_grants_when_none=preserve_existing_grants_when_none,
            )
        except AccessPermissionDeniedError as error:
            raise McpAdminRequiredError("mcp_admin_required") from error
        except InvalidVisibilityConfigError as error:
            raise ValueError(error.code) from error

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
