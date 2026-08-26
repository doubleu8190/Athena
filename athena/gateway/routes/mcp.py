"""MCP 服务器路由 — 注册 / 列出 / 注销 MCP 服务端.

请求体与用户输入的 mcp服务端s 格式完全一致：
    {"mcp服务端s": {"<name>": {"command": "...", "args": [...], "env": {...}}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from athena.models.mcp import McpServerConfig
from athena.runtime import runtime_from

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpServersPayload(BaseModel):
    """注册请求体 — 与用户输入的 mcp服务端s 格式一致."""

    mcpServers: dict[str, McpServerConfig]


class McpServerView(BaseModel):
    """已注册服务器视图（env 仅掩码，绝不返回原文）. 仅供展示."""

    name: str
    command: str
    args: list[str]
    env_masked: dict[str, str]
    status: str  # connected / failed（已连接 / 失败）
    tool_count: int
    error: str | None = None
    created_at: str


class McpRegisterResult(BaseModel):
    """单台服务器注册结果."""

    name: str
    status: str  # connected / failed（已连接 / 失败）
    tool_count: int
    error: str | None = None


@router.get("/servers")
async def list_mcp_servers(request: Request) -> dict[str, Any]:
    """列出所有已持久化的 MCP 服务器，附实时连接状态."""
    manager = runtime_from(request).mcp_manager
    items = await manager.list_servers()
    return {"items": items, "total": len(items)}


@router.post("/servers")
async def register_mcp_servers(payload: McpServersPayload, request: Request) -> dict[str, Any]:
    """注册 MCP 服务器（可一次注册多个）.

    逐台注册，单台失败不影响其它；始终返回 HTTP 200，状态在 results 中逐台体现。
    """
    manager = runtime_from(request).mcp_manager
    results: list[McpRegisterResult] = []
    for name, config in payload.mcpServers.items():
        result = await manager.register_server(name, config)
        results.append(McpRegisterResult(**result))

    registered = sum(1 for r in results if r.status == "connected")
    return {
        "total": len(results),
        "registered": registered,
        "failed": len(results) - registered,
        "results": results,
    }


@router.delete("/servers/{name:path}")
async def unregister_mcp_server(name: str, request: Request) -> dict[str, Any]:
    """注销 MCP 服务器：移除工具 + 断开连接 + DB 软删除.

    {name:path} 用于处理含 / 或 @ 的服务器名（客户端需 URL 编码）。
    """
    runtime = runtime_from(request)
    manager = runtime.mcp_manager
    db = runtime.db
    if await db.mcp_servers.get(name) is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    await manager.unregister_server(name)
    return {"status": "deleted", "name": name}
