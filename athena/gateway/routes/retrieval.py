"""检索运行和候选轨迹查询路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from athena.container import get_runtime_container

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.get("/runs")
async def list_retrieval_runs(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    scope: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    agent_run_id: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """分页读取召回运行摘要。

    参数：
        request: 当前 HTTP 请求。
        limit: 每页最多返回的运行数量。
        offset: 分页偏移量。
        scope: 可选的召回范围。
        status: 可选的运行状态。
        session_id: 可选的会话标识。
        agent_run_id: 可选的 Agent 运行标识。
        q: 可选的查询文本模糊匹配。

    返回值：
        包含当前页运行摘要和总数的字典。

    异常：
        数据库读取失败时传播底层异常。
    """
    runtime = get_runtime_container(request)
    items, total = await runtime.retrieval_trace_reader.list_runs(
        limit=limit,
        offset=offset,
        scope=scope,
        status=status,
        session_id=session_id,
        agent_run_id=agent_run_id,
        query=q,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


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
    run = await runtime.retrieval_trace_reader.get_run_with_candidates(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="检索运行不存在")
    return run
