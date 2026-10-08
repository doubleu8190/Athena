"""图索引上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from domain.retrieval import GraphContextQueryPort

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult


class GraphContextProvider:
    """通过图查询端口获取与任务相关的节点内容。"""

    name = "graph"

    def __init__(
        self,
        query_port: GraphContextQueryPort,
        *,
        limit: int = 12,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._query_port = query_port
        self._limit = limit
        self._timeout_seconds = timeout_seconds

    async def acquire(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult:
        query = plan.graph_query or getattr(task, "goal", "")
        if not query:
            return ProviderResult(self.name, "succeeded")
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                rows = await self._query_port.query_context(
                    query, limit=min(self._limit, plan.max_items)
                )
            items = tuple(
                ContextItem(
                    self.name,
                    str(row.get("content", "")),
                    str(row.get("id", "")),
                    {"attachment_id": row.get("attachment_id")},
                )
                for row in rows
                if row.get("content")
            )
            return ProviderResult(
                self.name,
                "succeeded",
                items,
                candidate_count=len(rows),
                result_count=len(items),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except TimeoutError:
            return ProviderResult(
                self.name,
                "timeout",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message="graph retrieval timed out",
            )
        except Exception as exc:
            return ProviderResult(
                self.name,
                "failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )


__all__ = ["GraphContextProvider"]
