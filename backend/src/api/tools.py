"""普通用户与管理员工具目录接口。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from api.converter.tool import (
    admin_tool_page_to_response,
    admin_tool_to_response,
    public_tool_page_to_response,
    public_tool_to_response,
)
from api.dto.tool import (
    AdminToolBatchApprovalResponse,
    AdminToolListResponse,
    AdminToolResponse,
    PublicToolListResponse,
    PublicToolResponse,
    ToolApprovalPolicyRequest,
    ToolBatchApprovalRequest,
)
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.security import AdminAuth, CurrentAuth, verify_request_origin
from app.tool import ToolManager
from domain.mcp import McpServerNotFoundError
from domain.tool import TOOL_FIELD_UNSET, ToolMetadataPatch


def _ensure_pagination_query(request: Request) -> None:
    """在认证依赖之后拒绝重复或未声明的扩展 query 参数。"""
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    if len(names) != len(set(names)) or not set(names) <= {"page", "page_size"}:
        raise HTTPException(422, "mcp_query_invalid")


def _no_query(request: Request) -> None:
    """确保工具详情不接收隐式投影扩展参数。"""
    if request.query_params:
        raise HTTPException(422, "mcp_query_invalid")


def build_tools_router(manager: ToolManager) -> APIRouter:
    """创建公共与管理员工具目录路由。"""
    router = APIRouter(route_class=SafeMcpRoute)

    @router.get("/api/v1/tools", response_model=PublicToolListResponse)
    async def list_public_tools(
        actor: CurrentAuth,
        request: Request,
        response: Response,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> PublicToolListResponse:
        _ensure_pagination_query(request)
        try:
            result = await manager.list_public_tools(
                actor, offset=(page - 1) * page_size, limit=page_size
            )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return public_tool_page_to_response(result, page_number=page, page_size=page_size)

    @router.get("/api/v1/tools/{tool_id}", response_model=PublicToolResponse)
    async def get_public_tool(
        actor: CurrentAuth,
        request: Request,
        tool_id: Annotated[str, Path(min_length=1)],
    ) -> PublicToolResponse:
        _no_query(request)
        try:
            result = await manager.get_public_tool(actor, tool_id)
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return public_tool_to_response(result)

    @router.get("/api/v1/admin/tools", response_model=AdminToolListResponse)
    async def list_admin_tools(
        actor: AdminAuth,
        request: Request,
        response: Response,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> AdminToolListResponse:
        _ensure_pagination_query(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await manager.list_admin_tools(
                actor, offset=(page - 1) * page_size, limit=page_size
            )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return admin_tool_page_to_response(result, page_number=page, page_size=page_size)

    @router.get("/api/v1/admin/tools/{tool_id}", response_model=AdminToolResponse)
    async def get_admin_tool(
        actor: AdminAuth,
        request: Request,
        tool_id: Annotated[str, Path(min_length=1)],
        response: Response,
    ) -> AdminToolResponse:
        _no_query(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await manager.get_admin_tool(actor, tool_id)
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return admin_tool_to_response(result)

    # 静态路径 batch-approval 必须注册在动态路径 /{tool_id} 之前，避免被路径参数吞掉。
    @router.patch(
        "/api/v1/admin/tools/batch-approval",
        response_model=AdminToolBatchApprovalResponse,
        dependencies=[Depends(verify_request_origin)],
    )
    async def batch_update_admin_tool_approval(
        actor: AdminAuth,
        request: ToolBatchApprovalRequest,
        response: Response,
    ) -> AdminToolBatchApprovalResponse:
        """单事务原子批量切换目标 MCP 工具的审批开关，任一工具不存在则整批回滚。"""
        response.headers["Cache-Control"] = "no-store"
        try:
            items = await manager.batch_update_tool_approval(
                actor, request.tool_ids, request.requires_approval
            )
        except McpServerNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "mcp_tool_not_found") from error
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return AdminToolBatchApprovalResponse(
            updated_count=len(items),
            items=[admin_tool_to_response(item) for item in items],
        )

    @router.patch(
        "/api/v1/admin/tools/{tool_id}",
        response_model=AdminToolResponse,
        dependencies=[Depends(verify_request_origin)],
    )
    async def update_admin_tool_policy(
        actor: AdminAuth,
        tool_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: ToolApprovalPolicyRequest,
        response: Response,
    ) -> AdminToolResponse:
        """局部更新工具审批开关及业务化展示元信息；仅传 requires_approval 时保持完全向后兼容。"""
        response.headers["Cache-Control"] = "no-store"
        fields_set = request.model_fields_set
        if not fields_set:
            raise HTTPException(422, "mcp_request_invalid")
        if "requires_approval" in fields_set and request.requires_approval is None:
            raise HTTPException(422, "mcp_request_invalid")
        try:
            if fields_set == {"requires_approval"}:
                assert request.requires_approval is not None
                result = await manager.update_tool_approval_policy(
                    actor, tool_id, request.requires_approval
                )
            else:
                patch = ToolMetadataPatch(
                    requires_approval=(
                        TOOL_FIELD_UNSET
                        if "requires_approval" not in fields_set
                        else cast(bool, request.requires_approval)
                    ),
                    display_name=(
                        TOOL_FIELD_UNSET
                        if "display_name" not in fields_set
                        else request.display_name
                    ),
                    approval_summary_template=(
                        TOOL_FIELD_UNSET
                        if "approval_summary_template" not in fields_set
                        else request.approval_summary_template
                    ),
                    parameter_labels=(
                        TOOL_FIELD_UNSET
                        if "parameter_labels" not in fields_set
                        else request.parameter_labels
                    ),
                )
                result = await manager.update_tool_metadata(actor, tool_id, patch)
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return admin_tool_to_response(result)

    return router


__all__ = ["build_tools_router"]
