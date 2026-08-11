"""工具管理路由 — 列出工具、启用/停用工具."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from athena.core.tools.manager import UnifiedToolManager, get_tool_manager
from athena.db.database import Database, get_database
from athena.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInfoView(BaseModel):
    """工具管理页展示的工具信息."""

    name: str
    description: str
    risk_level: str
    execution_mode: str
    require_approval: bool
    enabled: bool
    parameters: dict[str, Any] = {}
    last_called_at: str | None = None  # 最近调用时间(ISO)，来自 tool_call 表


class UpdateToolRequest(BaseModel):
    enabled: bool


async def _get_db() -> Database:
    from athena.config.settings import get_settings
    return await get_database(get_settings().sqlite_db_path)


def _manager_or_503() -> UnifiedToolManager:
    """获取工具管理器，未初始化时抛出 503."""
    manager = get_tool_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Tool manager not initialized")
    return manager


@router.get("")
async def list_tools() -> dict[str, Any]:
    """列出全部工具（含停用），附带治理信息与最近调用时间、今日调用统计."""
    manager = _manager_or_503()

    db = await _get_db()
    last_called = await db.tool_calls.last_called_by_tool()
    calls_today = await db.tool_calls.count_calls_since(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    )

    items: list[ToolInfoView] = []
    high_risk = 0
    for tool in manager.list_tools():
        schema = tool.schema
        risk = str(schema.risk_level.value)
        if risk == "high":
            high_risk += 1
        items.append(
            ToolInfoView(
                name=schema.name,
                description=schema.description,
                risk_level=risk,
                execution_mode=str(schema.execution_mode.value),
                require_approval=schema.require_approval,
                enabled=manager.is_enabled(schema.name),
                parameters=schema.parameters or {},
                last_called_at=last_called.get(schema.name),
            )
        )

    return {
        "items": items,
        "total": len(items),
        "enabled": sum(1 for i in items if i.enabled),
        "high_risk": high_risk,
        "calls_today": calls_today,
    }


@router.patch("/{name}")
async def update_tool(name: str, req: UpdateToolRequest) -> dict[str, Any]:
    """启用/停用工具. 未注册返回 404."""
    manager = _manager_or_503()
    if manager.get_tool(name) is None:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not registered")
    manager.set_enabled(name, req.enabled)
    return {"status": "updated", "name": name, "enabled": req.enabled}
