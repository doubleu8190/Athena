"""持久化 Run 编排事实查询 Controller。"""

from __future__ import annotations


from fastapi import APIRouter, HTTPException

from application.orchestration.query_service import OrchestrationQueryService
from interfaces.http.dto.orchestration import (
    RunOrchestrationSnapshotResponse,
)


def build_orchestration_router(
    orchestration_query_service: OrchestrationQueryService,
) -> APIRouter:
    """注册 canonical Run 编排快照路由。"""
    router = APIRouter(tags=["orchestration"])

    @router.get(
        "/runs/{run_id}/orchestration",
        response_model=RunOrchestrationSnapshotResponse,
    )
    async def get_run_orchestration(
        run_id: str,
    ) -> RunOrchestrationSnapshotResponse:
        value = await orchestration_query_service.run_snapshot(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        return RunOrchestrationSnapshotResponse.model_validate(value)

    return router


__all__ = ["build_orchestration_router"]
