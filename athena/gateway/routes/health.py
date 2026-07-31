"""健康检查路由."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """健康检查端点."""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
    }
