"""MCP API 的固定安全错误映射。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from domain.mcp import (
    McpAdminRequiredError,
    McpAuthorizationChangedError,
    McpCatalogPersistenceError,
    McpConfigVersionConflictError,
    McpDiscoveryError,
    McpDiscoveryTimeoutError,
    McpInvalidSessionError,
    McpSecretError,
    McpServerNameConflictError,
    McpServerNotFoundError,
    McpToolConflictError,
)


class SafeMcpRoute(APIRoute):
    """避免框架默认 422 响应回显 MCP 请求中的凭据或连接配置。"""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original_handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": "mcp_request_invalid"},
                )

        return safe_handler


def mcp_error_to_http(error: Exception) -> HTTPException:
    """仅用固定错误码映射已知异常，不暴露异常文本。"""
    if isinstance(error, McpInvalidSessionError):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    if isinstance(error, (McpAdminRequiredError, McpAuthorizationChangedError)):
        return HTTPException(status.HTTP_403_FORBIDDEN, "admin_required")
    if isinstance(error, McpServerNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "resource_not_found")
    if isinstance(
        error,
        (McpServerNameConflictError, McpConfigVersionConflictError, McpToolConflictError),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, "mcp_conflict")
    if isinstance(error, McpDiscoveryTimeoutError):
        return HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "mcp_discovery_timeout")
    if isinstance(error, McpDiscoveryError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "mcp_discovery_failed")
    if isinstance(error, (McpCatalogPersistenceError, McpSecretError)):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "mcp_unavailable")
    if isinstance(error, (ValueError, TypeError)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "mcp_request_invalid")
    raise error


__all__ = ["SafeMcpRoute", "mcp_error_to_http"]
