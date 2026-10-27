"""图索引上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from domain.retrieval import GraphContextQueryPort
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
)

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider


class GraphContextProvider(ContextProvider):
    """通过图查询端口获取与当前检索请求相关的节点内容。"""

    name = "graph"

    def __init__(
        self,
        query_port: GraphContextQueryPort,
        *,
        limit: int = 12,
        timeout_seconds: float = 5.0,
        active_revision_filter: Callable[[list[dict[str, object]]], Any] | None = None,
    ) -> None:
        self._query_port = query_port
        self._limit = limit
        self._timeout_seconds = timeout_seconds
        self._active_revision_filter = active_revision_filter

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        query = plan.graph_query
        if not query:
            return ProviderResult(self.name, "succeeded", query=query)
        started = time.perf_counter()
        limit = min(self._limit, plan.max_items)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                rows: list[dict[str, object]] = []
                cursor: str | None = None
                batch_size = max(32, limit)
                while len(rows) < limit:
                    batch = await self._query_port.search_nodes(
                        query, limit=batch_size, after_id=cursor
                    )
                    if not batch:
                        break
                    eligible = (
                        await self._active_revision_filter(batch)
                        if self._active_revision_filter is not None else batch
                    )
                    rows.extend(eligible[:limit - len(rows)])
                    next_cursor = str(batch[-1].get("id") or "")
                    if len(batch) < batch_size or not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
            traces: list[RetrievalCandidateTrace] = []
            items: list[ContextItem] = []
            for rank, row in enumerate(rows, 1):
                content = str(row.get("content") or "")
                native_id = str(row.get("id") or row.get("path_id") or "").strip()
                source_id = native_id or f"missing_graph_id_{rank}"
                is_path = bool(row.get("path_id"))
                metadata = {
                    key: value
                    for key, value in row.items()
                    if key not in {"id", "path_id", "content", "score"}
                }
                score = _score(row.get("score"))
                traces.append(
                    RetrievalCandidateTrace(
                        provider=self.name,
                        source_type="graph_path" if is_path else "graph_node",
                        source_id=source_id,
                        revision_id=_optional_text(row.get("revision_id")),
                        logical_source_id=str(row.get("attachment_id") or "") or None,
                        native_rank=rank,
                        native_score=score,
                        selected_for_result=bool(content and native_id),
                        filter_reason=(
                            "empty_content"
                            if not content
                            else "missing_source_id"
                            if not native_id
                            else None
                        ),
                        content_preview=content[:500] or None,
                        source_title=str(row.get("title") or "") or None,
                        locator={
                            key: value
                            for key, value in metadata.items()
                            if key in {"attachment_id", "path"}
                        },
                        metadata=metadata,
                        hits=(
                            RetrievalHitTrace(
                                channel="graph",
                                rank=rank,
                                score=score,
                            ),
                        ),
                        selection_stage=(
                            "provider" if not content or not native_id else None
                        ),
                    )
                )
                if content and native_id:
                    items.append(
                        ContextItem(self.name, content, native_id, metadata)
                    )
            return ProviderResult(
                self.name,
                "succeeded",
                tuple(items),
                candidate_count=len(traces),
                result_count=len(items),
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidates=tuple(traces),
                query=query,
                effective_params={"result_limit": limit},
                channels=(
                    RetrievalChannelTrace(
                        channel="graph",
                        status="succeeded",
                        hit_count=len(rows),
                    ),
                ),
            )
        except TimeoutError:
            return ProviderResult(
                self.name,
                "timeout",
                error_message="graph retrieval timed out",
                duration_ms=round((time.perf_counter() - started) * 1000),
                query=query,
                effective_params={"result_limit": limit},
                channels=(
                    RetrievalChannelTrace(
                        channel="graph",
                        status="timeout",
                        hit_count=0,
                        error_message="graph retrieval timed out",
                    ),
                ),
            )
        except Exception as exc:
            return ProviderResult(
                self.name,
                "failed",
                error_message=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000),
                query=query,
                effective_params={"result_limit": limit},
                channels=(
                    RetrievalChannelTrace(
                        channel="graph",
                        status="failed",
                        hit_count=0,
                        error_message=str(exc),
                    ),
                ),
            )


def _score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["GraphContextProvider"]
