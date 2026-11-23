"""将工具目录领域安全投影转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.tool import (
    AdminToolListResponse,
    AdminToolResponse,
    PublicToolListResponse,
    PublicToolResponse,
)
from domain.tool import AdminToolDefinition, PublicToolDefinition, ToolPage


def public_tool_to_response(tool: PublicToolDefinition) -> PublicToolResponse:
    """显式映射公共字段，避免透传领域对象。"""
    return PublicToolResponse(
        tool_id=tool.tool_id,
        model_name=tool.model_name,
        description=tool.description,
        input_schema=dict(tool.input_schema),
    )


def admin_tool_to_response(tool: AdminToolDefinition) -> AdminToolResponse:
    """显式映射管理员工具来源与业务审批展示字段。"""
    return AdminToolResponse(
        tool_id=tool.tool_id,
        model_name=tool.model_name,
        description=tool.description,
        input_schema=dict(tool.input_schema),
        server_id=tool.server_id,
        remote_name=tool.remote_name,
        requires_approval=tool.requires_approval,
        display_name=tool.display_name,
        approval_summary_template=tool.approval_summary_template,
        parameter_labels=dict(tool.parameter_labels),
    )


def public_tool_page_to_response(
    page: ToolPage[PublicToolDefinition], *, page_number: int, page_size: int
) -> PublicToolListResponse:
    """转换公共工具分页。"""
    return PublicToolListResponse(
        items=[public_tool_to_response(item) for item in page.items],
        total=page.total,
        page=page_number,
        page_size=page_size,
    )


def admin_tool_page_to_response(
    page: ToolPage[AdminToolDefinition], *, page_number: int, page_size: int
) -> AdminToolListResponse:
    """转换管理员工具分页。"""
    return AdminToolListResponse(
        items=[admin_tool_to_response(item) for item in page.items],
        total=page.total,
        page=page_number,
        page_size=page_size,
    )


__all__ = [
    "admin_tool_page_to_response",
    "admin_tool_to_response",
    "public_tool_page_to_response",
    "public_tool_to_response",
]
