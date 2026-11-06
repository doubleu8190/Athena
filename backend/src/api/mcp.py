"""管理员 MCP Server 配置与来源目录接口。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, ValidationError

from api.converter.mcp import mcp_server_page_to_response, mcp_server_to_response
from api.converter.tool import admin_tool_page_to_response
from api.dto.mcp import (
    McpServerCreateRequest,
    McpServerListResponse,
    McpServerResponse,
    McpServerUpdateRequest,
)
from api.dto.tool import AdminToolListResponse
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.security import AdminAuth, verify_request_origin
from app.tool import ToolManager
from domain.mcp import StdioConfig, StreamableHttpConfig


def _request_body_schema(model: type[BaseModel]) -> dict[str, Any]:
    """生成完整 JSON 请求契约，同时表达 transport 条件字段要求。"""
    schema = model.model_json_schema()
    schema["allOf"] = [
        {
            "if": {
                "properties": {"transport": {"const": "streamable_http"}},
                "required": ["transport"],
            },
            "then": {
                "required": ["url"],
                "properties": {"url": {"type": "string"}},
                "not": {
                    "anyOf": [
                        {"required": ["command"]},
                        {"required": ["args"]},
                        {"required": ["cwd"]},
                    ]
                },
            },
        },
        {
            "if": {
                "properties": {"transport": {"const": "stdio"}},
                "required": ["transport"],
            },
            "then": {
                "required": ["command", "args"],
                "properties": {
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "not": {"required": ["url"]},
            },
        },
    ]
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


async def _read_request_model[RequestModel: BaseModel](
    request: Request, model: type[RequestModel]
) -> RequestModel:
    """在认证依赖执行之后解析请求体，并只返回固定校验错误。"""
    try:
        body = await request.json()
        return model.model_validate(body)
    except (ValueError, UnicodeError, ValidationError):
        raise HTTPException(422, "mcp_request_invalid") from None


def _config(
    request: McpServerCreateRequest | McpServerUpdateRequest,
) -> StreamableHttpConfig | StdioConfig:
    if request.transport == "streamable_http":
        if request.url is None:
            raise ValueError("mcp_config_invalid")
        return StreamableHttpConfig(transport="streamable_http", url=request.url)
    if request.command is None:
        raise ValueError("mcp_config_invalid")
    return StdioConfig(
        transport="stdio",
        command=request.command,
        args=tuple(request.args or ()),
        cwd=request.cwd,
    )


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
        openapi_extra=_request_body_schema(McpServerCreateRequest),
    )
    async def register_server(
        http_request: Request,
        response: Response,
        actor: AdminAuth,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        request = await _read_request_model(http_request, McpServerCreateRequest)
        try:
            server = await manager.register_mcp(
                actor, request.name, _config(request), request.secrets
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
        openapi_extra=_request_body_schema(McpServerUpdateRequest),
    )
    async def update_server(
        http_request: Request,
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        request = await _read_request_model(http_request, McpServerUpdateRequest)
        try:
            result = await manager.update_mcp(
                actor,
                server_id,
                request.expected_config_version,
                request.name,
                _config(request),
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
