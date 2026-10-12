"""检索运行历史的只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.retrieval.query_ports import RetrievalQueryPort
from application.common.responses import RetrievalCandidateResponse, RetrievalRunResponse


class RetrievalQueryService:
    def __init__(self, port: RetrievalQueryPort) -> None:
        self._port = port

    async def list_runs(self, **filters: Any) -> tuple[list[RetrievalRunResponse], int]:
        values, total = await self._port.list_runs(**filters)
        return ([_run_response(value) for value in values], total)

    async def get_run(self, run_id: str) -> RetrievalRunResponse | None:
        value = await self._port.get_run(run_id)
        return None if value is None else _run_response(value)


def _run_response(value: dict[str, Any]) -> RetrievalRunResponse:
    """将检索端口返回的结构化记录转换为应用响应。"""
    candidates = value.get("candidates") or ()
    return RetrievalRunResponse(
        run_id=str(value.get("run_id", "")),
        query=str(value.get("query", "")),
        scope=str(value.get("scope", "")),
        status=str(value.get("status", "running")),
        candidates=tuple(
            RetrievalCandidateResponse(
                source_id=str(item.get("source_id", item.get("id", ""))),
                content=str(item.get("content", "")),
                provider=str(item.get("provider", "unknown")),
                score=float(item.get("score", 0.0)),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in candidates
        ),
    )


__all__ = ["RetrievalQueryService"]
