"""MCP ORM 行到安全领域投影的显式转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.mcp.model import McpServerModel, McpToolModel
from domain.mcp import McpSecretMetadata, McpServer, parse_server_config
from domain.tool import AdminToolDefinition, PublicToolDefinition, ToolDefinition


def _as_utc(value: datetime) -> datetime:
    """补齐 SQLite 测试驱动省略的 UTC 时区。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def server_to_domain(row: McpServerModel, secret_names: tuple[str, ...]) -> McpServer:
    """转换 Server 行和已由仓储认证解密的秘密名称，不接触秘密值。"""
    metadata = tuple(McpSecretMetadata(name=name, has_value=True) for name in secret_names)
    return McpServer(
        id=row.id,
        name=row.name,
        config=parse_server_config(row.config_json),
        secret_metadata=metadata,
        key_version=row.key_version,
        config_version=row.config_version,
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        tools_discovered_at=(
            _as_utc(row.tools_discovered_at) if row.tools_discovered_at is not None else None
        ),
    )


def tool_to_domain(row: McpToolModel) -> ToolDefinition:
    """将工具 ORM 行转换为内部定义。"""
    return ToolDefinition(
        tool_id=row.id,
        server_id=row.server_id,
        remote_name=row.remote_name,
        model_name=row.model_name,
        description=row.description,
        input_schema=row.input_schema_json,
    )


def tool_to_public(row: McpToolModel) -> PublicToolDefinition:
    """生成不含 MCP 来源映射的公共工具投影。"""
    return PublicToolDefinition(
        tool_id=row.id,
        model_name=row.model_name,
        description=row.description,
        input_schema=row.input_schema_json,
    )


def tool_to_admin(row: McpToolModel) -> AdminToolDefinition:
    """生成含来源映射但不含配置和秘密的管理员工具投影。"""
    return AdminToolDefinition(
        tool_id=row.id,
        model_name=row.model_name,
        description=row.description,
        input_schema=row.input_schema_json,
        server_id=row.server_id,
        remote_name=row.remote_name,
    )


__all__ = ["server_to_domain", "tool_to_admin", "tool_to_domain", "tool_to_public"]
