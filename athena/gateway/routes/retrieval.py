"""检索运行和候选轨迹查询路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from athena.container import get_runtime_container

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.get("/runs/{run_id}")
async def get_retrieval_run(run_id: str, request: Request) -> dict[str, Any]:
    """读取一次检索的配置、候选轨迹和最终注入状态。

    参数：
        run_id: 检索运行 ID。
        request: 当前 HTTP 请求。

    返回值：
        包含运行统计和候选轨迹的字典。

    异常：
        HTTPException: 检索运行不存在时返回 404。
    """
    runtime = get_runtime_container(request)
    run = await runtime.db.retrieval.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="检索运行不存在")
    return {**run, "candidates": await runtime.db.retrieval.list_candidates(run_id)}
