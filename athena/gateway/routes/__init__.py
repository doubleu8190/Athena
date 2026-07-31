"""REST API 路由聚合 — 各模块路由注册到统一 router."""

from __future__ import annotations

from fastapi import APIRouter

from athena.gateway.routes.approval import router as approval_router
from athena.gateway.routes.health import router as health_router
from athena.gateway.routes.memory import router as memory_router
from athena.gateway.routes.sessions import router as sessions_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(sessions_router)
api_router.include_router(approval_router)
api_router.include_router(memory_router)

__all__ = ["api_router"]
