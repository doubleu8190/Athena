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
    """按 DTO 白名单投影公共字段，同时将只读映射转为独立 dict。"""
    return PublicToolResponse.model_validate(tool)


def admin_tool_to_response(tool: AdminToolDefinition) -> AdminToolResponse:
    """按 DTO 白名单投影管理员工具来源与业务审批展示字段。"""
    return AdminToolResponse.model_validate(tool)


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
