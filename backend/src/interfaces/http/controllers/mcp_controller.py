"""目标 MCP Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from application.tools import MCPService
from domain.tools import MCPServerConfig


class MCPServerRequest(BaseModel):
    command: str | list[str]
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    image_id: str | None = None
    network_policy: str = "none"
    enabled: bool = True


def build_mcp_router(factory: Callable[[], MCPService]) -> APIRouter:
    """构建 MCP 服务端管理路由。"""
    router = APIRouter(prefix="/mcp", tags=["mcp"])

    @router.get("/servers")
    async def list_servers() -> dict[str, Any]:
        values = await factory().list_servers()
        return {"items": values, "total": len(values)}

    @router.post("/servers")
    async def register_servers(payload: dict[str, MCPServerRequest]) -> dict[str, Any]:
        results = []
        for name, item in payload.items():
            config = MCPServerConfig(item.command if isinstance(item.command, str) else tuple(item.command), tuple(item.args), dict(item.env), item.image_id, item.network_policy, item.enabled)
            results.append(await factory().register(name, config))
        registered = sum(value.get("status") == "connected" for value in results)
        return {"total": len(results), "registered": registered, "failed": len(results) - registered, "results": results}

    @router.delete("/servers/{name:path}")
    async def unregister_server(name: str) -> dict[str, str]:
        await factory().unregister(name)
        return {"status": "deleted", "name": name}

    return router


__all__ = ["build_mcp_router"]
