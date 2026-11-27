"""MCP API 的固定安全错误映射与脱敏路由包装。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from api.errors import DomainErrorRoute, domain_error_to_http


class SafeMcpRoute(DomainErrorRoute):
    """在统一领域异常转换外层阻止 422 校验响应回显 MCP 凭据或连接配置。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": "mcp_request_invalid"},
                )
            except (ValueError, TypeError) as error:
                # 兜底映射 MCP 路径上的普通参数类型/值校验异常，不回显原始文本。
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="mcp_request_invalid",
                ) from error

        return safe_handler


def mcp_error_to_http(error: Exception) -> HTTPException:
    """仅用固定错误码映射已知 MCP 异常，不暴露异常文本。"""
    mapped = domain_error_to_http(error)
    if mapped is not None:
        return mapped
    if isinstance(error, (ValueError, TypeError)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "mcp_request_invalid")
    raise error


__all__ = ["SafeMcpRoute", "mcp_error_to_http"]
