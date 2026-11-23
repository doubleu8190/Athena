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
    McpServerEnabledRequest,
    McpServerListResponse,
    McpServerPatchRequest,
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
from domain.mcp import McpConfigVersionConflictError

_CREATE_ADAPTER: TypeAdapter[HttpServerCreateRequest | StdioServerCreateRequest] = TypeAdapter(
    cast(Any, McpServerCreateRequest)
)
_UPDATE_ADAPTER: TypeAdapter[HttpServerUpdateRequest | StdioServerUpdateRequest] = TypeAdapter(
    cast(Any, McpServerUpdateRequest)
)
_PATCH_ADAPTER: TypeAdapter[McpServerPatchRequest] = TypeAdapter(McpServerPatchRequest)
_ENABLED_ADAPTER: TypeAdapter[McpServerEnabledRequest] = TypeAdapter(McpServerEnabledRequest)


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

    @router.patch(
        "/{server_id}",
        response_model=McpServerResponse,
        openapi_extra=_openapi_request_body(_PATCH_ADAPTER),
    )
    async def patch_server(
        http_request: Request,
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        """保留既有加密凭据的局部更新接口，可选触发远端工具重发现。"""
        request = await _read_request_model(http_request, _PATCH_ADAPTER)
        fields_set = request.model_fields_set
        # 拒绝显式将不可空核心字段置为 null。
        for field_name in ("name", "config", "visibility_scope", "enabled"):
            if field_name in fields_set and getattr(request, field_name) is None:
                raise HTTPException(422, "mcp_request_invalid")
        try:
            result = await manager.patch_mcp(
                actor,
                server_id,
                request.expected_config_version,
                name=request.name if "name" in fields_set else None,
                config_patch=request.config if "config" in fields_set else None,
                secrets_upsert=(request.secrets_upsert if "secrets_upsert" in fields_set else None),
                secrets_remove=(request.secrets_remove if "secrets_remove" in fields_set else None),
                visibility_scope=(
                    VisibilityScope(request.visibility_scope)
                    if "visibility_scope" in fields_set and request.visibility_scope is not None
                    else None
                ),
                owner_department_id=request.owner_department_id,
                owner_department_id_set=("owner_department_id" in fields_set),
                visibility_grants=(
                    None
                    if request.visibility_grants is None
                    else tuple(item.to_domain() for item in request.visibility_grants)
                ),
                visibility_grants_set=("visibility_grants" in fields_set),
                enabled=request.enabled if "enabled" in fields_set else None,
                rediscover=request.rediscover,
            )
        except McpConfigVersionConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "mcp_config_version_conflict") from error
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_to_response(result)

    @router.put(
        "/{server_id}/enabled",
        response_model=McpServerResponse,
        openapi_extra=_openapi_request_body(_ENABLED_ADAPTER),
    )
    async def set_server_enabled(
        http_request: Request,
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        """原子切换 MCP Server 启停状态并递增配置版本号。"""
        request = await _read_request_model(http_request, _ENABLED_ADAPTER)
        try:
            result = await manager.set_mcp_enabled(
                actor,
                server_id,
                request.enabled,
                expected_config_version=request.expected_config_version,
            )
        except McpConfigVersionConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "mcp_config_version_conflict") from error
        except Exception as error:
            raise mcp_error_to_http(error) from error
        response.headers["Cache-Control"] = "no-store"
        return mcp_server_to_response(result)

    @router.post(
        "/{server_id}/rediscover",
        response_model=McpServerResponse,
    )
    async def rediscover_server(
        actor: AdminAuth,
        server_id: Annotated[str, Path(min_length=1)],
        response: Response,
        _: Annotated[None, Depends(verify_request_origin)],
    ) -> McpServerResponse:
        """使用已存加密凭据独立重发现远端工具并按 remote_name 增量对齐。"""
        try:
            result = await manager.rediscover_mcp(actor, server_id)
        except McpConfigVersionConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "mcp_config_version_conflict") from error
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
