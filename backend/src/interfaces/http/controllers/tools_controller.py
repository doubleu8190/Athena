"""目标工具治理 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from application.tools import ToolService


class ToolGovernanceRequest(BaseModel):
    enabled: bool | None = None
    risk_level: str | None = None
    require_approval: bool | None = None


def build_tools_router(factory: Callable[[], ToolService]) -> APIRouter:
    """构建工具治理路由。"""
    router = APIRouter(prefix="/tools", tags=["tools"])

    @router.get("")
    async def list_tools() -> dict[str, Any]:
        values = await factory().list()
        items = [
            {
                "name": value.tool_name,
                "description": value.description,
                "risk_level": value.risk_level.value,
                "execution_mode": value.execution_mode.value,
                "require_approval": value.require_approval,
                "enabled": value.enabled,
                "parameters": value.parameters.values,
            }
            for value in values
        ]
        return {"items": items, "total": len(items), "enabled": sum(item["enabled"] for item in items), "high_risk": sum(item["risk_level"] == "high" for item in items), "calls_today": 0}

    @router.patch("/{name}")
    async def update_tool(name: str, body: ToolGovernanceRequest) -> dict[str, Any]:
        if body.enabled is None and body.risk_level is None and body.require_approval is None:
            raise HTTPException(status_code=400, detail="at least one tool governance field is required")
        try:
            value = await factory().update_governance(
                name,
                risk_level=body.risk_level,
                require_approval=body.require_approval,
                enabled=body.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if value is None:
            raise HTTPException(status_code=404, detail="tool_not_found")
        return {"status": "updated", "name": value.tool_name, "enabled": value.enabled, "risk_level": value.risk_level.value, "require_approval": value.require_approval}

    return router


__all__ = ["build_tools_router"]
