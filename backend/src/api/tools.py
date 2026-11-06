"""普通用户与管理员工具目录接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response

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
)
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.security import AdminAuth, CurrentAuth
from app.tool import ToolManager


def _pagination(request: Request) -> tuple[int, int]:
    """在登录依赖之后严格解析分页参数，拒绝重复和扩展 query。"""
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    if len(names) != len(set(names)) or not set(names) <= {"page", "page_size"}:
        raise HTTPException(422, "mcp_query_invalid")
    values = dict(items)
    try:
        page = int(values.get("page", "1"))
        page_size = int(values.get("page_size", "20"))
    except ValueError:
        raise HTTPException(422, "mcp_query_invalid") from None
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "mcp_query_invalid")
    return page, page_size


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
    ) -> PublicToolListResponse:
        page, page_size = _pagination(request)
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
    ) -> AdminToolListResponse:
        page, page_size = _pagination(request)
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

    return router


__all__ = ["build_tools_router"]
