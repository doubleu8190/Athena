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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `query_port`（GraphContextQueryPort）：查询知识图谱上下文的应用端口；约束：无额外运行时限制。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `12`。
        - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：可省略，默认值为 `5.0`。
        - `active_revision_filter`（Callable[[list[dict[str, object]]], Any] | None）：限定有效知识版本的可选筛选条件；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        """查询知识图谱上下文，按计划和超时约束生成检索结果。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。

        返回：
        - ProviderResult：检索提供方的状态、候选结果和追踪信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
                # 分批越过失效版本，直到收集到足量当前版本节点或游标耗尽。
                while len(rows) < limit:
                    batch = await self._query_port.search_nodes(
                        query, limit=batch_size, after_id=cursor
                    )
                    if not batch:
                        break
                    # 版本过滤发生在补足条数前，避免旧节点占满本次检索上限。
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
    """将输入分数转换为有限、可比较的数值。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - float | None：归一化后的有限相关性分数；输入缺失或非数值时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    """将可选输入规范化为非空文本或 None。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - str | None：去除首尾空白的非空文本；输入不是字符串或为空时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["GraphContextProvider"]
