"""检索运行历史的只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.retrieval.query_ports import RetrievalPort
from application.common.responses import RetrievalCandidateResponse, RetrievalRunResponse


class RetrievalQueryService:
    """提供检索运行记录和追踪详情的查询用例。
    """
    def __init__(self, port: RetrievalPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `port`（RetrievalPort）：本用例依赖的应用端口，提供所需外部能力；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def list_runs(self, **filters: Any) -> tuple[list[RetrievalRunResponse], int]:
        """列出运行记录。

        参数：
        - `filters`（Any）：按字段和值筛选查询结果的附加条件；约束：无额外运行时限制。

        返回：
        - tuple[list[RetrievalRunResponse], int]：按筛选条件分页返回的检索运行响应列表及其总数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        values, total = await self._port.list_runs(**filters)
        return ([_run_response(value) for value in values], total)

    async def get_run(self, run_id: str) -> RetrievalRunResponse | None:
        """读取运行。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - RetrievalRunResponse | None：指定标识的检索运行响应；记录不存在或不可见时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await self._port.get_run(run_id)
        return None if value is None else _run_response(value)


def _run_response(value: dict[str, Any]) -> RetrievalRunResponse:
    """将检索端口返回的结构化记录转换为应用响应。

    参数：
    - `value`（dict[str, Any]）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - RetrievalRunResponse：供调用方展示或继续处理的 `RetrievalRunResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    candidates = value.get("candidates") or ()
    return RetrievalRunResponse(
        run_id=str(value.get("run_id", "")),
        query=str(value.get("query", "")),
        scope=str(value.get("scope", "")),
        status=str(value.get("status", "running")),
        session_id=value.get("session_id"),
        agent_run_id=value.get("agent_run_id"),
        message_id=value.get("message_id"),
        trace_status=str(value.get("trace_status", "incomplete")),
        raw_hit_count=value.get("raw_hit_count"),
        candidate_count=int(value.get("candidate_count", 0)),
        selected_count=int(value.get("selected_count", 0)),
        context_count=value.get("context_count"),
        selection_status=str(value.get("selection_status", "incomplete")),
        providers=tuple(dict(item) for item in value.get("providers", ()) if isinstance(item, dict)),
        config=dict(value.get("config") or {}),
        index_generation=value.get("index_generation"),
        duration_ms=value.get("duration_ms"),
        error_message=value.get("error_message"),
        created_at=str(value.get("created_at", "")),
        completed_at=value.get("completed_at"),
        candidates=tuple(
            RetrievalCandidateResponse(
                candidate_id=str(item.get("candidate_id", "")),
                run_id=str(item.get("run_id", value.get("run_id", ""))),
                provider=str(item.get("provider", "unknown")),
                stage=str(item.get("stage", "context")),
                source_type=str(item.get("source_type", "unknown")),
                source_id=str(item.get("source_id", item.get("id", ""))),
                logical_source_id=item.get("logical_source_id"),
                revision_id=item.get("revision_id"),
                native_rank=item.get("native_rank"),
                native_score=item.get("native_score"),
                fused_rank=item.get("fused_rank"),
                fused_score=item.get("fused_score"),
                rerank_rank=item.get("rerank_rank"),
                rerank_score=item.get("rerank_score"),
                selected_for_result=bool(item.get("selected_for_result", False)),
                filter_reason=item.get("filter_reason"),
                content_preview=item.get("content_preview"),
                source_title=item.get("source_title"),
                locator=dict(item.get("locator") or {}),
                hits=tuple(dict(hit) for hit in item.get("hits", ()) if isinstance(hit, dict)),
                context_selected=item.get("context_selected"),
                selection_stage=item.get("selection_stage"),
                metadata=dict(item.get("metadata") or {}),
                created_at=str(item.get("created_at", "")),
            )
            for item in candidates
        ),
    )


__all__ = ["RetrievalQueryService"]
