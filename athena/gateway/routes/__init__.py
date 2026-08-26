"""REST API 路由聚合 — 各模块路由注册到统一 router."""

from __future__ import annotations

from fastapi import APIRouter

from athena.gateway.routes.approval import router as approval_router
from athena.gateway.routes.health import router as health_router
from athena.gateway.routes.mcp import router as mcp_router
from athena.gateway.routes.memory import router as memory_router
from athena.gateway.routes.providers import router as providers_router
from athena.gateway.routes.sessions import router as sessions_router
from athena.gateway.routes.settings import router as settings_router
from athena.gateway.routes.tools import router as tools_router
from athena.gateway.routes.files import router as files_router
from athena.gateway.routes.evaluation import router as evaluation_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(sessions_router)
api_router.include_router(approval_router)
api_router.include_router(memory_router)
api_router.include_router(tools_router)
api_router.include_router(settings_router)
api_router.include_router(providers_router)
api_router.include_router(mcp_router)
api_router.include_router(files_router)
api_router.include_router(evaluation_router)

__all__ = ["api_router"]
