"""MCP Server 与工具快照的原子 SQLAlchemy 仓储。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from adapter.mcp.converter.mcp import server_to_domain, tool_to_admin, tool_to_public
from adapter.mcp.model import McpServerModel, McpToolModel
from common.ids import now_utc
from domain.auth import AuthContext, UserRole
from domain.mcp import (
    McpAuthorizationChangedError,
    McpCatalogPersistenceError,
    McpConfigVersionConflictError,
    McpSecretPurpose,
    McpServer,
    McpServerNameConflictError,
    McpServerNotFoundError,
    McpServerPage,
    McpServerRepository,
    McpServerRuntimeConfig,
    McpServerWrite,
    McpToolConflictError,
    McpTransport,
)
from domain.tool import (
    AdminToolDefinition,
    PublicToolDefinition,
    ToolCatalogReader,
    ToolDefinition,
    ToolPage,
)
from infrastructure.security.mcp_secrets import McpSecretCipher, SealedMcpSecrets


class SqlAlchemyMcpRepository(McpServerRepository, ToolCatalogReader):
    """在权限复核与资源更新间保持单一事务和统一锁顺序。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_cipher: McpSecretCipher,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存会话工厂、AES-GCM 封装器和可替换时钟。

        参数：session_factory 创建短事务 AsyncSession；secret_cipher 负责秘密封装；
        clock 返回 UTC 当前时刻。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._session_factory = session_factory
        self._secret_cipher = secret_cipher
        self._clock = clock

    async def create_with_tools(
        self,
        actor: AuthContext,
        server: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        """原子创建 Server 和工具快照，先复核身份再写入。

        参数：actor 是可信 user/session 身份；server 为 Server 配置与写入秘密；
        tools 为调用方提供的完整发现结果。
        返回：不含秘密明文的已保存 Server。
        异常：管理员资格变化、Server 名称冲突或工具约束冲突时抛领域异常。
        """
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
                        created_by=actor.user.id,
                        created_at=now,
                        updated_at=now,
                        tools_discovered_at=now,
                    )
                    session.add(row)
                    await session.flush()
                    self._insert_tools(session, server.id, tools, {}, now)
                    await session.flush()
                    return self._server_projection(row)
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
        """锁定 Server 后比较版本，并原子替换配置、秘密及工具快照。

        参数：actor 是可信 user/session 身份；server_id 为目标；expected_config_version
        是调用方读取的版本；replacement 和 tools 是完整的新快照。
        返回：替换后的安全 Server 投影。
        异常：授权变化、资源不存在、版本冲突或目录唯一性冲突时抛领域异常。
        """
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
                        .where(McpServerModel.id == server_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    if row.config_version != expected_config_version:
                        raise McpConfigVersionConflictError("mcp_config_version_conflict")
                    self._decrypt_secrets(row)
                    existing_rows = list(
                        (
                            await session.scalars(
                                select(McpToolModel).where(McpToolModel.server_id == server_id)
                            )
                        ).all()
                    )
                    existing_by_remote = {tool.remote_name: tool for tool in existing_rows}
                    row.name = replacement.name.strip()
                    row.transport = replacement.config.transport
                    row.config_json = self._config_json(replacement)
                    row.secrets_ciphertext = sealed.ciphertext if sealed else None
                    row.secrets_nonce = sealed.nonce if sealed else None
                    row.key_version = sealed.key_version if sealed else None
                    row.config_version += 1
                    row.updated_at = now
                    row.tools_discovered_at = now
                    desired_remote_names = {tool.remote_name for tool in tools}
                    for old_tool in existing_rows:
                        if old_tool.remote_name not in desired_remote_names:
                            await session.delete(old_tool)
                    self._replace_tools(
                        session,
                        server_id,
                        tools,
                        existing_by_remote,
                        now,
                    )
                    await session.flush()
                    return self._server_projection(row)
        except IntegrityError as error:
            self._raise_integrity_domain_error(error)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        """同一事务复核管理员并物理删除 Server，工具由外键级联清除。

        参数：actor 是可信 user/session 身份；server_id 是目标 Server。
        返回：删除成功时无返回值。
        异常：权限复核失败或 Server 不存在时抛领域异常。
        """
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = await session.scalar(
                        select(McpServerModel)
                        .where(McpServerModel.id == server_id)
                        .with_for_update()
                    )
                    if row is None:
                        raise McpServerNotFoundError("mcp_server_not_found")
                    await session.delete(row)
        except SQLAlchemyError:
            raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None

    async def get(self, server_id: str) -> McpServer | None:
        """读取单个 Server 的安全投影。

        参数：server_id 是 Server 主键。
        返回：脱敏 Server；不存在时返回 None。
        异常：数据库或密文元数据损坏时传播不含秘密值的领域异常。
        """
        async with self._session_factory() as session:
            row = await session.get(McpServerModel, server_id)
        return None if row is None else self._server_projection(row)

    async def get_runtime_config(self, server_id: str) -> McpServerRuntimeConfig | None:
        """读取连接配置并仅在内部 transport 边界解封秘密。

        参数：server_id 是 Server 主键。
        返回：供连接器使用的敏感连接配置；不存在时返回 None。
        异常：密钥版本不匹配、密文损坏或认证标签失败时抛 McpSecretError。
        """
        async with self._session_factory() as session:
            row = await session.get(McpServerModel, server_id)
        if row is None:
            return None
        secrets = self._decrypt_secrets(row)
        server = server_to_domain(row, tuple(sorted(secrets)))
        return McpServerRuntimeConfig(server=server, secrets=secrets)

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        """按更新时间倒序读取 Server 安全投影分页。

        参数：offset 是非负偏移；limit 是正数且不大于 100。
        返回：Server 项目及总数。
        异常：分页参数无效时抛 ValueError。
        """
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")
        async with self._session_factory() as session:
            total = int(await session.scalar(select(func.count()).select_from(McpServerModel)) or 0)
            rows = list(
                (
                    await session.scalars(
                        select(McpServerModel)
                        .order_by(McpServerModel.updated_at.desc(), McpServerModel.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
        return McpServerPage(items=tuple(self._server_projection(row) for row in rows), total=total)

    async def list_public(
        self,
        *,
        offset: int,
        limit: int,
    ) -> ToolPage[PublicToolDefinition]:
        """读取不包含 Server 来源信息的工具投影分页。"""
        rows, total = await self._tool_page(offset, limit)
        return ToolPage(items=tuple(tool_to_public(row) for row in rows), total=total)

    async def list_admin(
        self,
        *,
        offset: int,
        limit: int,
    ) -> ToolPage[AdminToolDefinition]:
        """读取包含来源映射但无连接配置和秘密的管理员工具分页。"""
        rows, total = await self._tool_page(offset, limit)
        return ToolPage(items=tuple(tool_to_admin(row) for row in rows), total=total)

    async def _tool_page(self, offset: int, limit: int) -> tuple[list[McpToolModel], int]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("mcp_page_invalid")
        async with self._session_factory() as session:
            total = int(await session.scalar(select(func.count()).select_from(McpToolModel)) or 0)
            rows = list(
                (
                    await session.scalars(
                        select(McpToolModel)
                        .order_by(McpToolModel.model_name)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
        return rows, total

    async def _require_admin(self, session: AsyncSession, actor: AuthContext) -> None:
        """在最终写事务中先锁 user/session 并读取当前角色。"""
        user = await SqlAlchemyAuthTransactionGuard(
            session, clock=self._clock
        ).lock_current_session_user(
            actor.user.id,
            actor.session_id,
        )
        if user is None or user.role is not UserRole.ADMIN:
            raise McpAuthorizationChangedError("mcp_authorization_changed")

    def _seal(self, server: McpServerWrite) -> SealedMcpSecrets | None:
        return (
            self._secret_cipher.seal(server.id, server.secret_purpose, server.secrets)
            if server.secrets
            else None
        )

    def _server_projection(self, row: McpServerModel) -> McpServer:
        """先认证解密秘密，再把经过验证的名称交给纯转换器。"""
        secrets = self._decrypt_secrets(row)
        return server_to_domain(row, tuple(sorted(secrets)))

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
        existing_by_remote: dict[str, McpToolModel],
        created_at: datetime,
    ) -> None:
        for tool in tools:
            existing = existing_by_remote.get(tool.remote_name)
            session.add(
                McpToolModel(
                    id=existing.id if existing else tool.tool_id,
                    server_id=server_id,
                    remote_name=tool.remote_name,
                    model_name=(existing.model_name if existing else tool.model_name),
                    description=tool.description,
                    input_schema_json=dict(tool.input_schema),
                    created_at=existing.created_at if existing else created_at,
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
        """更新保留项并插入新增项，保留已有 id、model name 与创建时间。"""
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
                        created_at=updated_at,
                    )
                )
            else:
                existing.description = tool.description
                existing.input_schema_json = dict(tool.input_schema)

    @staticmethod
    def _raise_integrity_domain_error(error: IntegrityError) -> NoReturn:
        """把已知唯一性冲突转成固定文案，不回显驱动参数。"""
        message = str(error.orig).lower()
        if "uq_mcp_servers_name" in message or "mcp_servers.name" in message:
            raise McpServerNameConflictError("mcp_server_name_conflict") from None
        if "uq_mcp_tools" in message or "mcp_tools." in message:
            raise McpToolConflictError("mcp_tool_conflict") from None
        raise McpCatalogPersistenceError("mcp_catalog_persistence_failed") from None


__all__ = ["SqlAlchemyMcpRepository"]
