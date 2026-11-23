"""管理员 MCP Server 配置与来源目录接口。"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import TypeAdapter, ValidationError

from api.converter.mcp import mcp_server_page_to_response, mcp_server_to_response
from api.converter.tool import admin_tool_page_to_response
from api.dto.mcp import (
    HttpServerCreateRequest,
    HttpServerUpdateRequest,
    McpServerCreateRequest,
    McpServerListResponse,
    McpServerResponse,
    McpServerUpdateRequest,
    StdioServerCreateRequest,
    StdioServerUpdateRequest,
)
from api.dto.tool import AdminToolListResponse
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.security import AdminAuth, verify_request_origin
from app.tool import ToolManager
from domain.access import VisibilityScope

_CREATE_ADAPTER: TypeAdapter[
    HttpServerCreateRequest | StdioServerCreateRequest
] = TypeAdapter(cast(Any, McpServerCreateRequest))
_UPDATE_ADAPTER: TypeAdapter[
    HttpServerUpdateRequest | StdioServerUpdateRequest
] = TypeAdapter(cast(Any, McpServerUpdateRequest))


def _openapi_request_body(adapter: TypeAdapter[Any]) -> dict[str, Any]:
    """基于 Pydantic 判别联合直接生成 OpenAPI requestBody 契约。"""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": adapter.json_schema()}},
        }
    }


async def _read_request_model[RequestT](
    request: Request, adapter: TypeAdapter[RequestT]
) -> RequestT:
    """在认证依赖执行之后解析请求体，并只返回脱敏后的固定校验错误。"""
    try:
        body = await request.json()
        return adapter.validate_python(body)
    except (ValueError, UnicodeError, ValidationError):
        raise HTTPException(422, "mcp_request_invalid") from None


def build_mcp_router(manager: ToolManager) -> APIRouter:
    """创建管理员 MCP API 路由。"""
    router = APIRouter(
        prefix="/api/v1/admin/mcp/servers",
        tags=["admin-mcp"],
        route_class=SafeMcpRoute,
    )

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=McpServerResponse,
        openapi_extra=_openapi_request_body(_CREATE_ADAPTER),
    )
    async def register_server(
        http_request: Request,
        response: Response,
        actor: AdminAuth,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        request = await _read_request_model(http_request, _CREATE_ADAPTER)
        has_visibility = bool(
            request.model_fields_set
            & {"visibility_scope", "owner_department_id", "visibility_grants"}
        )
        try:
            if has_visibility:
                server = await manager.register_mcp(
                    actor,
                    request.name,
                    request.to_domain_config(),
                    request.secrets,
                    visibility_scope=VisibilityScope(request.visibility_scope),
                    owner_department_id=request.owner_department_id,
                    visibility_grants=(
                        None
                        if request.visibility_grants is None
                        else tuple(item.to_domain() for item in request.visibility_grants)
                    ),
                )
            else:
                server = await manager.register_mcp(
                    actor, request.name, request.to_domain_config(), request.secrets
                )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Location"] = f"/api/v1/admin/mcp/servers/{server.id}"
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_to_response(server)

    @router.get("", response_model=McpServerListResponse)
    async def list_servers(
        actor: AdminAuth,
        response: Response,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> McpServerListResponse:
        try:
            result = await manager.list_mcp_servers(
                actor, offset=(page - 1) * page_size, limit=page_size
            )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_page_to_response(result, page_number=page, page_size=page_size)

    @router.get("/{server_id}", response_model=McpServerResponse)
    async def get_server(
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
    ) -> McpServerResponse:
        try:
            result = await manager.get_mcp_server(actor, server_id)
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_to_response(result)

    @router.put(
        "/{server_id}",
        response_model=McpServerResponse,
        openapi_extra=_openapi_request_body(_UPDATE_ADAPTER),
    )
    async def update_server(
        http_request: Request,
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        request = await _read_request_model(http_request, _UPDATE_ADAPTER)
        has_visibility = bool(
            request.model_fields_set
            & {"visibility_scope", "owner_department_id", "visibility_grants"}
        )
        try:
            if has_visibility:
                result = await manager.update_mcp(
                    actor,
                    server_id,
                    request.expected_config_version,
                    request.name,
                    request.to_domain_config(),
                    request.secrets,
                    visibility_scope=VisibilityScope(request.visibility_scope),
                    owner_department_id=request.owner_department_id,
                    visibility_grants=(
                        None
                        if request.visibility_grants is None
                        else tuple(item.to_domain() for item in request.visibility_grants)
                    ),
                )
            else:
                result = await manager.update_mcp(
                    actor,
                    server_id,
                    request.expected_config_version,
                    request.name,
                    request.to_domain_config(),
                    request.secrets,
                )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_to_response(result)

    @router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_server(
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> Response:
        try:
            await manager.unregister_mcp(actor, server_id)
        except Exception as error:
            raise mcp_error_to_http(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/{server_id}/tools", response_model=AdminToolListResponse)
    async def list_server_tools(
        actor: AdminAuth,
        response: Response,
        server_id: Annotated[str, Path(min_length=1)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> AdminToolListResponse:
        try:
            result = await manager.list_server_tools(
                actor, server_id, offset=(page - 1) * page_size, limit=page_size
            )
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return admin_tool_page_to_response(result, page_number=page, page_size=page_size)

    return router


__all__ = ["build_mcp_router"]
