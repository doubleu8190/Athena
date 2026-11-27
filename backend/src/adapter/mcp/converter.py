"""MCP ORM 行到安全领域投影的显式转换。"""

from __future__ import annotations

from adapter.mcp.model import McpServerModel, McpToolModel
from common.date import as_utc
from domain.access import VisibilityGrant, VisibilityScope
from domain.mcp import McpSecretMetadata, McpServer, parse_server_config
from domain.tool import AdminToolDefinition, PublicToolDefinition


def server_to_domain(
    row: McpServerModel,
    secret_names: tuple[str, ...],
    *,
    grants: tuple[VisibilityGrant, ...] = (),
) -> McpServer:
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
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        tools_discovered_at=(
            as_utc(row.tools_discovered_at) if row.tools_discovered_at is not None else None
        ),
        visibility_scope=VisibilityScope(row.visibility_scope),
        owner_department_id=row.owner_department_id,
        visibility_grants=grants,
        enabled=True if row.enabled is None else bool(row.enabled),
    )


def tool_to_public(row: McpToolModel) -> PublicToolDefinition:
    """生成不含模型路由名、入参 Schema 与来源映射的公共最小工具投影。"""
    return PublicToolDefinition(
        tool_id=row.id,
        display_name=row.display_name,
        description=row.description,
    )


def tool_to_admin(row: McpToolModel) -> AdminToolDefinition:
    """生成含来源映射与业务审批展示元信息但不含配置和秘密的管理员工具投影。"""
    return AdminToolDefinition(
        tool_id=row.id,
        model_name=row.model_name,
        description=row.description,
        input_schema=row.input_schema_json,
        server_id=row.server_id,
        remote_name=row.remote_name,
        requires_approval=row.requires_approval,
        display_name=row.display_name,
        approval_summary_template=row.approval_summary_template,
        parameter_labels=dict(row.parameter_labels_json or {}),
        result_labels=dict(row.result_labels_json or {}),
    )


__all__ = ["server_to_domain", "tool_to_admin", "tool_to_public"]
