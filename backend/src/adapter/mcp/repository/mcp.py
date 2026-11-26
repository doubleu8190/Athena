"""MCP Server 与工具快照的原子 SQLAlchemy 仓储。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import NoReturn

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import (
    build_readable_resource_clause,
    delete_resource_grants_in_session,
    load_grants_map_in_session,
    load_user_access_snapshot_in_session,
    sync_resource_grants_in_session,
)
from adapter.auth.repository.transaction_guard import verify_active_session_user
from adapter.mcp.converter.mcp import (
    server_to_domain,
    tool_to_admin,
    tool_to_public,
)
from adapter.mcp.model import McpServerModel, McpToolModel
from common.ids import now_utc
from domain.access import ResourceType, UserAccessSnapshot, VisibilityGrant, VisibilityScope
from domain.auth import AuthContext, UserRole
from domain.mcp import (
    McpAuthorizationChangedError,
    McpCatalogPersistenceError,
    McpConfigVersionConflictError,
    McpConnectionInput,
    McpRuntimeToolTarget,
    McpSecretPurpose,
    McpServer,
    McpServerConnectionState,
    McpServerNameConflictError,
    McpServerNotFoundError,
    McpServerPage,
    McpServerRepository,
    McpServerWrite,
    McpToolConflictError,
    McpToolRuntimeReader,
    McpTransport,
)
from domain.tool import (
    TOOL_FIELD_UNSET,
    AdminToolDefinition,
    McpToolSource,
    PublicToolDefinition,
    RuntimeToolDefinition,
    ToolDefinition,
    ToolMetadataPatch,
    ToolPage,
    validate_approval_summary_template,
    validate_parameter_labels,
    validate_tool_display_name,
)
from infrastructure.security.mcp_secrets import McpSecretCipher, SealedMcpSecrets


class SqlAlchemyMcpRepository(McpServerRepository, McpToolRuntimeReader):
    """在权限复核与资源更新间保持单一事务和统一锁顺序。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_cipher: McpSecretCipher,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存会话工厂、AES-GCM 封装器和可替换时钟。"""
        self._session_factory = session_factory
        self._secret_cipher = secret_cipher
        self._clock = clock

    async def create_with_tools(
        self,
        actor: AuthContext,
        server: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """原子创建 Server 和工具快照，先复核身份再写入。"""
        sealed = self._seal(server)
        now = self._clock()
        self._validate_tools(server.id, tools)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = McpServerModel(
                        id=server.id,
                        name=server.name.strip(),
                        transport=server.config.transport,
                        config_json=self._config_json(server),
                        secrets_ciphertext=sealed.ciphertext if sealed else None,
                        secrets_nonce=sealed.nonce if sealed else None,
                        key_version=sealed.key_version if sealed else None,
                        config_version=1,
                        enabled=True,
                        visibility_scope=server.visibility_scope.value,
                        owner_department_id=server.owner_department_id,
                        created_by=actor.user.id,
                        deleted_at=None,
                        created_at=now,
                        updated_at=now,
                        tools_discovered_at=now,
                    )
                    session.add(row)
                    await session.flush()
                    self._insert_tools(session, server.id, tools, now)
                    grants = await sync_resource_grants_in_session(
                        session,
                        ResourceType.MCP_SERVER,
                        server.id,
                        (
                            server.visibility_grants
                            if server.visibility_scope is VisibilityScope.DEPARTMENT
                            else ()
                        ),
                        now=now,
                    )
                    await session.flush()
                    # 新写入密文来自当前已校验明文 server.secrets，直接透传键名避免刚加密又解密。
                    return self._server_projection(
                        row,
                        secret_names=tuple(sorted(server.secrets)),
                        grants=grants,
                    )
        except IntegrityError as error:
            self._raise_integrity_domain_error(error)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def replace_with_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        replacement: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """锁定未软删 Server 后比较版本，并原子替换配置、秘密及工具快照。"""
        if replacement.id != server_id or expected_config_version < 1:
            raise ValueError("mcp_config_invalid")
        sealed = self._seal(replacement)
        now = self._clock()
        self._validate_tools(server_id, tools)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(
                            McpServerModel.id == server_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    if row.config_version != expected_config_version:
                        raise McpConfigVersionConflictError("mcp_config_version_conflict")
                    self._decrypt_secrets(row)
                    row.name = replacement.name.strip()
                    row.transport = replacement.config.transport
                    row.config_json = self._config_json(replacement)
                    row.secrets_ciphertext = sealed.ciphertext if sealed else None
                    row.secrets_nonce = sealed.nonce if sealed else None
                    row.key_version = sealed.key_version if sealed else None
                    row.visibility_scope = replacement.visibility_scope.value
                    row.owner_department_id = replacement.owner_department_id
                    row.config_version += 1
                    row.updated_at = now
                    row.tools_discovered_at = now
                    await self._align_server_tools_in_session(session, server_id, tools, now)
                    grants = await sync_resource_grants_in_session(
                        session,
                        ResourceType.MCP_SERVER,
                        server_id,
                        (
                            replacement.visibility_grants
                            if replacement.visibility_scope is VisibilityScope.DEPARTMENT
                            else ()
                        ),
                        now=now,
                    )
                    await session.flush()
                    # 既有密文已于替换前认证校验，此处直接透传新写入的 replacement.secrets 键名。
                    return self._server_projection(
                        row,
                        secret_names=tuple(sorted(replacement.secrets)),
                        grants=grants,
                    )
        except IntegrityError as error:
            self._raise_integrity_domain_error(error)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

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
        """CAS 锁行后原子写入保密合并配置；rediscovered_tools 为 None 时保留既有工具目录。"""
        if replacement.id != server_id or expected_config_version < 1 or type(enabled) is not bool:
            raise ValueError("mcp_config_invalid")
        sealed = self._seal(replacement)
        now = self._clock()
        if rediscovered_tools is not None:
            self._validate_tools(server_id, rediscovered_tools)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(
                            McpServerModel.id == server_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    if row.config_version != expected_config_version:
                        raise McpConfigVersionConflictError("mcp_config_version_conflict")
                    # 先校验既有密文完整性，避免损坏密文被静默覆盖。
                    self._decrypt_secrets(row)
                    row.name = replacement.name.strip()
                    row.transport = replacement.config.transport
                    row.config_json = self._config_json(replacement)
                    row.secrets_ciphertext = sealed.ciphertext if sealed else None
                    row.secrets_nonce = sealed.nonce if sealed else None
                    row.key_version = sealed.key_version if sealed else None
                    row.visibility_scope = replacement.visibility_scope.value
                    row.owner_department_id = replacement.owner_department_id
                    row.enabled = enabled
                    row.config_version += 1
                    row.updated_at = now
                    if rediscovered_tools is not None:
                        # rediscover=True 时按 remote_name 增量对齐工具，完整保留存活工具元信息。
                        row.tools_discovered_at = now
                        await self._align_server_tools_in_session(
                            session, server_id, rediscovered_tools, now
                        )
                    if sync_grants:
                        grants = await sync_resource_grants_in_session(
                            session,
                            ResourceType.MCP_SERVER,
                            server_id,
                            (
                                replacement.visibility_grants
                                if replacement.visibility_scope is VisibilityScope.DEPARTMENT
                                else ()
                            ),
                            now=now,
                        )
                    else:
                        grants_map = await load_grants_map_in_session(
                            session, ResourceType.MCP_SERVER, (server_id,)
                        )
                        grants = grants_map.get(server_id, ())
                    await session.flush()
                    # 既有密文已于替换前认证校验，此处直接透传新写入的 replacement.secrets 键名。
                    return self._server_projection(
                        row,
                        secret_names=tuple(sorted(replacement.secrets)),
                        grants=grants,
                    )
        except IntegrityError as error:
            self._raise_integrity_domain_error(error)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def set_enabled(
        self,
        actor: AuthContext,
        server_id: str,
        enabled: bool,
        *,
        expected_config_version: int | None = None,
    ) -> McpServer:
        """单事务锁行切换未软删 Server 启停状态并递增 config_version。"""
        if type(enabled) is not bool:
            raise ValueError("mcp_config_invalid")
        if expected_config_version is not None and (
            type(expected_config_version) is not int or expected_config_version < 1
        ):
            raise ValueError("mcp_config_invalid")
        now = self._clock()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(
                            McpServerModel.id == server_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    if (
                        expected_config_version is not None
                        and row.config_version != expected_config_version
                    ):
                        raise McpConfigVersionConflictError("mcp_config_version_conflict")
                    secrets = self._decrypt_secrets(row)
                    row.enabled = enabled
                    row.updated_at = now
                    row.config_version += 1
                    await session.flush()
                    grants_map = await load_grants_map_in_session(
                        session, ResourceType.MCP_SERVER, (server_id,)
                    )
                    # 启停操作未修改密文，复用已认证解密得到的键名避免同一事务内重复解密。
                    return self._server_projection(
                        row,
                        secret_names=tuple(sorted(secrets)),
                        grants=grants_map.get(server_id, ()),
                    )
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def sync_discovered_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """结合读取期 config_version 做 CAS 保护，增量对齐远端重发现工具并保留存活元信息。"""
        if expected_config_version < 1:
            raise ValueError("mcp_config_invalid")
        now = self._clock()
        self._validate_tools(server_id, tools)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(
                            McpServerModel.id == server_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    if row.config_version != expected_config_version:
                        raise McpConfigVersionConflictError("mcp_config_version_conflict")
                    secrets = self._decrypt_secrets(row)
                    await self._align_server_tools_in_session(session, server_id, tools, now)
                    row.tools_discovered_at = now
                    row.updated_at = now
                    row.config_version += 1
                    await session.flush()
                    grants_map = await load_grants_map_in_session(
                        session, ResourceType.MCP_SERVER, (server_id,)
                    )
                    # 工具同步未修改密文，复用已认证解密得到的键名避免同一事务内重复解密。
                    return self._server_projection(
                        row,
                        secret_names=tuple(sorted(secrets)),
                        grants=grants_map.get(server_id, ()),
                    )
        except IntegrityError as error:
            self._raise_integrity_domain_error(error)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        """单事务软删除 Server、清空密文、删除名下全部工具并清理可见性授权。"""
        now = self._clock()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(
                            McpServerModel.id == server_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    # 清理授权与名下全部工具，释放 model_name 唯一约束并确保工具即刻下线。
                    await delete_resource_grants_in_session(
                        session, ResourceType.MCP_SERVER, server_id
                    )
                    await session.execute(
                        delete(McpToolModel).where(McpToolModel.server_id == server_id)
                    )
                    # 软删除并彻底清空密文凭据三元组，满足 ck_mcp_servers_secret_bundle。
                    row.deleted_at = now
                    row.enabled = False
                    row.updated_at = now
                    row.config_version += 1
                    row.secrets_ciphertext = None
                    row.secrets_nonce = None
                    row.key_version = None
                    await session.flush()
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def set_requires_approval(
        self, actor: AuthContext, tool_id: str, requires_approval: bool
    ) -> AdminToolDefinition:
        """复核管理员后更新未软删 Server 下单个工具的审批策略。"""
        if not tool_id or len(tool_id) > 64 or type(requires_approval) is not bool:
            raise ValueError("mcp_tool_policy_invalid")
        return await self.update_tool_metadata(
            actor,
            tool_id,
            ToolMetadataPatch(requires_approval=requires_approval),
        )

    async def update_tool_metadata(
        self, actor: AuthContext, tool_id: str, patch: ToolMetadataPatch
    ) -> AdminToolDefinition:
        """复核管理员后原子更新单个工具的审批开关与业务化审批展示元信息。"""
        if not tool_id or len(tool_id) > 64 or not patch.has_updates():
            raise ValueError("mcp_tool_policy_invalid")
        if patch.requires_approval is not TOOL_FIELD_UNSET and (
            type(patch.requires_approval) is not bool
        ):
            raise ValueError("mcp_tool_policy_invalid")
        normalized_display_name = (
            TOOL_FIELD_UNSET
            if patch.display_name is TOOL_FIELD_UNSET
            else validate_tool_display_name(patch.display_name)
        )
        normalized_template = (
            TOOL_FIELD_UNSET
            if patch.approval_summary_template is TOOL_FIELD_UNSET
            else validate_approval_summary_template(patch.approval_summary_template)
        )
        normalized_labels = (
            TOOL_FIELD_UNSET
            if patch.parameter_labels is TOOL_FIELD_UNSET
            else validate_parameter_labels(patch.parameter_labels)
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpToolModel)
                        .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                        .where(
                            McpToolModel.id == tool_id,
                            McpServerModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_tool_not_found")
                    if patch.requires_approval is not TOOL_FIELD_UNSET:
                        row.requires_approval = patch.requires_approval
                    if normalized_display_name is not TOOL_FIELD_UNSET:
                        row.display_name = normalized_display_name
                    if normalized_template is not TOOL_FIELD_UNSET:
                        row.approval_summary_template = normalized_template
                    if normalized_labels is not TOOL_FIELD_UNSET:
                        row.parameter_labels_json = normalized_labels
                    await session.flush()
                    return tool_to_admin(row)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def batch_set_requires_approval(
        self,
        actor: AuthContext,
        tool_ids: Sequence[str],
        requires_approval: bool,
    ) -> tuple[AdminToolDefinition, ...]:
        """单事务原子批量更新工具审批开关；若任一工具不存在或所属 Server 已删，整批回滚。"""
        ordered_ids = tuple(tool_ids)
        if (
            not 1 <= len(ordered_ids) <= 100
            or len(set(ordered_ids)) != len(ordered_ids)
            or type(requires_approval) is not bool
            or any(not item or len(item) > 64 for item in ordered_ids)
        ):
            raise ValueError("mcp_tool_policy_invalid")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    # 按主键顺序加行锁，避免并发批量更新产生交叉死锁。
                    rows = list(
                        (
                            await session.scalars(
                                select(McpToolModel)
                                .join(
                                    McpServerModel,
                                    McpServerModel.id == McpToolModel.server_id,
                                )
                                .where(
                                    McpToolModel.id.in_(ordered_ids),
                                    McpServerModel.deleted_at.is_(None),
                                )
                                .order_by(McpToolModel.id)
                                .with_for_update()
                            )
                        ).all()
                    )
                    if len(rows) != len(ordered_ids):
                        raise McpServerNotFoundError("mcp_tool_not_found")
                    by_id = {row.id: row for row in rows}
                    for row in rows:
                        row.requires_approval = requires_approval
                    await session.flush()
                    return tuple(tool_to_admin(by_id[item_id]) for item_id in ordered_ids)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def get(self, server_id: str) -> McpServer | None:
        """读取单个未软删 Server 的安全投影。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(McpServerModel).where(
                    McpServerModel.id == server_id,
                    McpServerModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            grants_map = await load_grants_map_in_session(
                session, ResourceType.MCP_SERVER, (row.id,)
            )
            return self._server_projection(row, grants=grants_map.get(row.id, ()))

    async def get_connection_state(self, server_id: str) -> McpServerConnectionState | None:
        """读取未软删 Server 及已解封凭据，仅供内部保密局部更新和独立重发现使用。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(McpServerModel).where(
                    McpServerModel.id == server_id,
                    McpServerModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            secrets = self._decrypt_secrets(row)
            grants_map = await load_grants_map_in_session(
                session, ResourceType.MCP_SERVER, (row.id,)
            )
            server = server_to_domain(
                row,
                tuple(sorted(secrets)),
                grants=grants_map.get(row.id, ()),
            )
            return McpServerConnectionState(server=server, secrets=secrets)

    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
        """读取运行态可用工具：仅包含未软删且已启用（enabled=True）的 Server 工具。"""
        if not 1 <= limit <= 201:
            raise ValueError("mcp_runtime_tool_limit_invalid")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(McpToolModel, McpServerModel)
                    .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                    .where(
                        McpServerModel.deleted_at.is_(None),
                        McpServerModel.enabled.is_(True),
                    )
                    .order_by(McpToolModel.server_id, McpToolModel.model_name)
                    .limit(limit)
                )
            ).all()
        return tuple(self._runtime_tool_projection(tool, server) for tool, server in rows)

    async def list_readable_runtime_tools(
        self,
        snapshot: UserAccessSnapshot | None,
        *,
        limit: int = 201,
    ) -> tuple[RuntimeToolDefinition, ...]:
        """单 Session 以 SQL EXISTS 下推读取当前用户快照可读的已启用运行时 MCP 工具。

        同时修复先 LIMIT 201 再内存过滤导致未授权部门 MCP 占用有效工具额度的问题。
        """
        if not 1 <= limit <= 201:
            raise ValueError("mcp_runtime_tool_limit_invalid")
        if snapshot is None or not snapshot.is_active:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(McpToolModel, McpServerModel)
                    .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                    .where(
                        McpServerModel.deleted_at.is_(None),
                        McpServerModel.enabled.is_(True),
                        build_readable_resource_clause(
                            snapshot,
                            ResourceType.MCP_SERVER,
                            id_col=McpServerModel.id,
                            scope_col=McpServerModel.visibility_scope,
                            owner_id_col=None,
                            owner_department_id_col=McpServerModel.owner_department_id,
                        ),
                    )
                    .order_by(McpToolModel.server_id, McpToolModel.model_name)
                    .limit(limit)
                )
            ).all()
        return tuple(self._runtime_tool_projection(tool, server) for tool, server in rows)

    @staticmethod
    def _runtime_tool_projection(
        tool: McpToolModel,
        server: McpServerModel,
    ) -> RuntimeToolDefinition:
        """将 ORM 联表行投影为运行时工具定义，保持与全量读取一致。"""
        return RuntimeToolDefinition(
            tool_id=tool.id,
            model_name=tool.model_name,
            description=tool.description,
            input_schema=dict(tool.input_schema_json),
            source=McpToolSource(
                server_id=server.id,
                remote_name=tool.remote_name,
                config_version=server.config_version,
            ),
            requires_approval=tool.requires_approval,
            display_name=tool.display_name,
            approval_summary_template=tool.approval_summary_template,
            parameter_labels=dict(tool.parameter_labels_json or {}),
        )

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        """校验工具存在、Server 未软删且已启用、配置版本一致后解封单次调用目标。"""
        if expected_config_version < 1:
            return None
        async with self._session_factory() as session:
            pair = (
                await session.execute(
                    select(McpToolModel, McpServerModel)
                    .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                    .where(
                        McpToolModel.id == tool_id,
                        McpToolModel.server_id == server_id,
                        McpToolModel.remote_name == remote_name,
                        McpServerModel.deleted_at.is_(None),
                        McpServerModel.enabled.is_(True),
                    )
                )
            ).one_or_none()
        if pair is None:
            return None
        tool, server = pair
        if server.config_version != expected_config_version:
            return None
        secrets = self._decrypt_secrets(server)
        server_domain = server_to_domain(server, tuple(sorted(secrets)))
        return McpRuntimeToolTarget(
            tool_id=tool.id,
            server_id=server.id,
            remote_name=tool.remote_name,
            config_version=server.config_version,
            connection=McpConnectionInput(server_domain.config, secrets),
        )

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        """按更新时间倒序读取未软删 Server 安全投影分页。"""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")
        async with self._session_factory() as session:
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(McpServerModel)
                    .where(McpServerModel.deleted_at.is_(None))
                )
                or 0
            )
            rows = list(
                (
                    await session.scalars(
                        select(McpServerModel)
                        .where(McpServerModel.deleted_at.is_(None))
                        .order_by(McpServerModel.updated_at.desc(), McpServerModel.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.MCP_SERVER, [row.id for row in rows]
            )
        return McpServerPage(
            items=tuple(
                self._server_projection(row, grants=grants_map.get(row.id, ())) for row in rows
            ),
            total=total,
        )

    async def list_readable_public(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
        snapshot: UserAccessSnapshot | None = None,
    ) -> ToolPage[PublicToolDefinition]:
        """单 Session 加载/复用用户权限快照并以 SQL EXISTS 下推读取可读且已启用工具分页。"""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return ToolPage(items=(), total=0)
        if snapshot is not None and not snapshot.is_active:
            return ToolPage(items=(), total=0)
        async with self._session_factory() as session:
            resolved = (
                snapshot
                if snapshot is not None
                else await load_user_access_snapshot_in_session(session, normalized_user_id)
            )
            if resolved is None or not resolved.is_active:
                return ToolPage(items=(), total=0)
            readable_clause = build_readable_resource_clause(
                resolved,
                ResourceType.MCP_SERVER,
                id_col=McpServerModel.id,
                scope_col=McpServerModel.visibility_scope,
                owner_id_col=None,
                owner_department_id_col=McpServerModel.owner_department_id,
            )
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(McpToolModel)
                    .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                    .where(
                        McpServerModel.deleted_at.is_(None),
                        McpServerModel.enabled.is_(True),
                        readable_clause,
                    )
                )
                or 0
            )
            if total == 0:
                return ToolPage(items=(), total=0)
            rows = list(
                (
                    await session.scalars(
                        select(McpToolModel)
                        .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                        .where(
                            McpServerModel.deleted_at.is_(None),
                            McpServerModel.enabled.is_(True),
                            readable_clause,
                        )
                        .order_by(McpToolModel.model_name)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
        return ToolPage(items=tuple(tool_to_public(row) for row in rows), total=total)

    async def list_public(
        self,
        *,
        offset: int,
        limit: int,
        allowed_server_ids: Sequence[str] | None = None,
    ) -> ToolPage[PublicToolDefinition]:
        """读取普通用户可见的已启用 Server 工具分页，停用或软删 Server 的工具立即摘除。"""
        if allowed_server_ids is not None and not allowed_server_ids:
            return ToolPage(items=(), total=0)
        rows, total = await self._tool_page(
            offset,
            limit,
            allowed_server_ids=allowed_server_ids,
            only_enabled_servers=True,
        )
        return ToolPage(items=tuple(tool_to_public(row) for row in rows), total=total)

    async def list_admin(
        self,
        *,
        offset: int,
        limit: int,
    ) -> ToolPage[AdminToolDefinition]:
        """读取全部未软删 Server 的管理员工具分页（含停用 Server，便于后台运维）。"""
        rows, total = await self._tool_page(offset, limit, only_enabled_servers=False)
        return ToolPage(items=tuple(tool_to_admin(row) for row in rows), total=total)

    async def list_server_admin(
        self, server_id: str, *, offset: int, limit: int
    ) -> ToolPage[AdminToolDefinition]:
        """读取指定未软删 Server 的管理员工具分页。"""
        rows, total = await self._tool_page(
            offset, limit, server_id=server_id, only_enabled_servers=False
        )
        return ToolPage(items=tuple(tool_to_admin(row) for row in rows), total=total)

    async def get_public_tool(self, tool_id: str) -> PublicToolDefinition | None:
        """按工具 ID 返回未软删且已启用 Server 的公共工具投影。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(McpToolModel)
                .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                .where(
                    McpToolModel.id == tool_id,
                    McpServerModel.deleted_at.is_(None),
                    McpServerModel.enabled.is_(True),
                )
            )
        return None if row is None else tool_to_public(row)

    async def get_admin_tool(self, tool_id: str) -> AdminToolDefinition | None:
        """按工具 ID 返回未软删 Server 的管理员工具投影。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(McpToolModel)
                .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                .where(
                    McpToolModel.id == tool_id,
                    McpServerModel.deleted_at.is_(None),
                )
            )
        return None if row is None else tool_to_admin(row)

    async def _tool_page(
        self,
        offset: int,
        limit: int,
        *,
        server_id: str | None = None,
        allowed_server_ids: Sequence[str] | None = None,
        only_enabled_servers: bool = False,
    ) -> tuple[list[McpToolModel], int]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")
        async with self._session_factory() as session:
            count_query = (
                select(func.count())
                .select_from(McpToolModel)
                .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                .where(McpServerModel.deleted_at.is_(None))
            )
            rows_query = (
                select(McpToolModel)
                .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                .where(McpServerModel.deleted_at.is_(None))
            )
            if only_enabled_servers:
                count_query = count_query.where(McpServerModel.enabled.is_(True))
                rows_query = rows_query.where(McpServerModel.enabled.is_(True))
            if server_id is not None:
                count_query = count_query.where(McpToolModel.server_id == server_id)
                rows_query = rows_query.where(McpToolModel.server_id == server_id)
            if allowed_server_ids is not None:
                unique_ids = tuple(dict.fromkeys(allowed_server_ids))
                count_query = count_query.where(McpToolModel.server_id.in_(unique_ids))
                rows_query = rows_query.where(McpToolModel.server_id.in_(unique_ids))
            total = int(await session.scalar(count_query) or 0)
            rows = list(
                (
                    await session.scalars(
                        rows_query.order_by(McpToolModel.model_name).offset(offset).limit(limit)
                    )
                ).all()
            )
        return rows, total

    async def _align_server_tools_in_session(
        self,
        session: AsyncSession,
        server_id: str,
        tools: Sequence[ToolDefinition],
        now: datetime,
    ) -> None:
        """按 remote_name 增量对齐工具快照，完整保留存活工具 ID、审批开关与业务展示元信息。"""
        existing_rows = list(
            (
                await session.scalars(
                    select(McpToolModel).where(McpToolModel.server_id == server_id)
                )
            ).all()
        )
        existing_by_remote = {tool.remote_name: tool for tool in existing_rows}
        desired_remote_names = {tool.remote_name for tool in tools}
        removed = False
        for old_tool in existing_rows:
            if old_tool.remote_name not in desired_remote_names:
                await session.delete(old_tool)
                removed = True
        if removed:
            # 先 flush 删除行，避免后续插入在唯一约束上产生中间冲突。
            await session.flush()
        self._replace_tools(session, server_id, tools, existing_by_remote, now)

    async def _require_admin(self, session: AsyncSession, actor: AuthContext) -> None:
        """在写事务中无锁复核当前登录会话与超管角色，避免对认证表加行锁。"""
        user = await verify_active_session_user(
            session,
            actor.user.id,
            actor.session_id,
            clock=self._clock,
        )
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise McpAuthorizationChangedError("mcp_authorization_changed")

    def _seal(self, server: McpServerWrite) -> SealedMcpSecrets | None:
        return (
            self._secret_cipher.seal(server.id, server.secret_purpose, server.secrets)
            if server.secrets
            else None
        )

    def _server_projection(
        self,
        row: McpServerModel,
        *,
        secret_names: tuple[str, ...] | None = None,
        grants: tuple[VisibilityGrant, ...] = (),
    ) -> McpServer:
        """投影为领域 McpServer；未提供 secret_names 时执行认证解密（损坏密文 fail-closed）。"""
        resolved_names = (
            tuple(sorted(self._decrypt_secrets(row))) if secret_names is None else secret_names
        )
        return server_to_domain(row, resolved_names, grants=grants)

    def _decrypt_secrets(self, row: McpServerModel) -> dict[str, str]:
        """校验密文三元组并认证解密；缺失或损坏时失败关闭。"""
        if row.secrets_ciphertext is None:
            if row.secrets_nonce is not None or row.key_version is not None:
                raise McpCatalogPersistenceError("mcp_catalog_persistence_failed")
            return {}
        if row.secrets_nonce is None or row.key_version is None:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed")
        purpose = (
            McpSecretPurpose.HTTP_HEADERS
            if row.transport == McpTransport.STREAMABLE_HTTP.value
            else McpSecretPurpose.STDIO_ENVIRONMENT
        )
        return dict(
            self._secret_cipher.open(
                row.id,
                purpose,
                row.secrets_ciphertext,
                row.secrets_nonce,
                row.key_version,
            )
        )

    @staticmethod
    def _config_json(server: McpServerWrite) -> dict[str, str | list[str] | None]:
        config = server.config
        if config.transport == "streamable_http":
            return {"transport": config.transport, "url": config.url}
        return {
            "transport": config.transport,
            "command": config.command,
            "args": list(config.args),
            "cwd": config.cwd,
        }

    @staticmethod
    def _validate_tools(server_id: str, tools: Sequence[ToolDefinition]) -> None:
        remote_names: set[str] = set()
        for tool in tools:
            if tool.server_id != server_id or tool.remote_name in remote_names:
                raise McpToolConflictError("mcp_tool_conflict")
            remote_names.add(tool.remote_name)

    @staticmethod
    def _insert_tools(
        session: AsyncSession,
        server_id: str,
        tools: Sequence[ToolDefinition],
        created_at: datetime,
    ) -> None:
        for tool in tools:
            session.add(
                McpToolModel(
                    id=tool.tool_id,
                    server_id=server_id,
                    remote_name=tool.remote_name,
                    model_name=tool.model_name,
                    description=tool.description,
                    input_schema_json=dict(tool.input_schema),
                    requires_approval=tool.requires_approval,
                    display_name=None,
                    approval_summary_template=None,
                    parameter_labels_json={},
                    created_at=created_at,
                )
            )

    @staticmethod
    def _replace_tools(
        session: AsyncSession,
        server_id: str,
        tools: Sequence[ToolDefinition],
        existing_by_remote: dict[str, McpToolModel],
        updated_at: datetime,
    ) -> None:
        """更新保留项并插入新增项，完整保留存活工具的 id、model_name、审批开关与展示元信息。"""
        for tool in tools:
            existing = existing_by_remote.get(tool.remote_name)
            if existing is None:
                session.add(
                    McpToolModel(
                        id=tool.tool_id,
                        server_id=server_id,
                        remote_name=tool.remote_name,
                        model_name=tool.model_name,
                        description=tool.description,
                        input_schema_json=dict(tool.input_schema),
                        requires_approval=tool.requires_approval,
                        display_name=None,
                        approval_summary_template=None,
                        parameter_labels_json={},
                        created_at=updated_at,
                    )
                )
            else:
                existing.description = tool.description
                existing.input_schema_json = dict(tool.input_schema)

    @staticmethod
    def _raise_integrity_domain_error(error: IntegrityError) -> NoReturn:
        """按 PostgreSQL 约束名把已知唯一性冲突转成固定文案，不回显驱动参数。"""
        orig = error.orig
        # 优先读取驱动原生的 PostgreSQL 约束名（兼容 asyncpg __cause__.constraint_name），
        # 未提供时退回匹配驱动异常文本中的 PostgreSQL 约束名。
        constraint_name = getattr(orig, "constraint_name", None) or getattr(
            getattr(orig, "__cause__", None),
            "constraint_name",
            None,
        )
        target = (
            constraint_name.lower()
            if isinstance(constraint_name, str) and constraint_name
            else str(orig or error).lower()
        )
        if "uq_mcp_servers_name" in target:
            raise McpServerNameConflictError("mcp_server_name_conflict") from None
        if "uq_mcp_tools" in target:
            raise McpToolConflictError("mcp_tool_conflict") from None
        raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None


__all__ = ["SqlAlchemyMcpRepository"]
