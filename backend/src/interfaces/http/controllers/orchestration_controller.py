"""持久化 Run 编排事实查询 Controller。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from application.orchestration.query_service import OrchestrationQueryService
from interfaces.http.dto.orchestration import (
    PlanVersionResponse,
    RunOrchestrationSnapshotResponse,
)


def build_orchestration_router(
    query_service_factory: Callable[[], OrchestrationQueryService],
) -> APIRouter:
    """注册 Run 快照和不可变 Plan Version 查询路由。"""
    router = APIRouter(tags=["orchestration"])

    def query_service() -> OrchestrationQueryService:
        return query_service_factory()

    @router.get(
        "/runs/{run_id}/orchestration",
        response_model=RunOrchestrationSnapshotResponse,
    )
    async def get_run_orchestration(
        run_id: str,
        queries: OrchestrationQueryService = Depends(query_service),
    ) -> RunOrchestrationSnapshotResponse:
        value = await queries.run_snapshot(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        return RunOrchestrationSnapshotResponse.model_validate(value)

    @router.get(
        "/plans/{plan_id}/versions/{version}",
        response_model=PlanVersionResponse,
    )
    async def get_plan_version(
        plan_id: str,
        version: int,
        queries: OrchestrationQueryService = Depends(query_service),
    ) -> PlanVersionResponse:
        if version < 1:
            raise HTTPException(status_code=404, detail="plan_version_not_found")
        value = await queries.plan_version(plan_id, version)
        if value is None:
            raise HTTPException(status_code=404, detail="plan_version_not_found")
        return PlanVersionResponse.model_validate(value)

    return router


__all__ = ["build_orchestration_router"]
