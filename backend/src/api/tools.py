"""普通用户与管理员工具目录接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from api.converter.tool import (
    admin_tool_page_to_response,
    admin_tool_to_response,
    public_tool_page_to_response,
    public_tool_to_response,
)
from api.dto.tool import (
    AdminToolListResponse,
    AdminToolResponse,
    PublicToolListResponse,
    PublicToolResponse,
    ToolApprovalPolicyRequest,
)
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.security import AdminAuth, CurrentAuth, verify_request_origin
from app.tool import ToolManager


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
        """Update only the administrator-owned approval policy."""
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await manager.update_tool_approval_policy(
                actor, tool_id, request.requires_approval
            )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return admin_tool_to_response(result)

    return router


__all__ = ["build_tools_router"]
