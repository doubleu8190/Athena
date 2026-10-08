from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import ContextPlan


class ContextPlanner:
    """把任务理解结果转换成带查询和范围的 ContextPlan。"""

    def __init__(self, *, max_files: int = 5, limit_per_file: int = 5, max_items: int = 12, max_tokens: int = 4000, recall_limit: int = 30, rerank_limit: int = 10) -> None:
        self._max_files = max_files
        self._limit_per_file = limit_per_file
        self._max_items = max_items
        self._max_tokens = max_tokens
        self._recall_limit = recall_limit
        self._rerank_limit = rerank_limit

    def plan(self, task: Any, *, requested_file_ids: list[str] | tuple[str, ...] = ()) -> ContextPlan:
        def value(name: str, default: Any = None) -> Any:
            if isinstance(task, Mapping):
                return task.get(name, default)
            return getattr(task, name, default)

        requirements = list(dict.fromkeys(value("context_requirements") or ("conversation",)))
        if requested_file_ids and "file" not in requirements:
            requirements.append("file")
        hints = value("query_hints")
        if isinstance(hints, Mapping):
            hint = lambda name: hints.get(name, "")
        else:
            hint = lambda name: getattr(hints, name, "") if hints else ""
        return ContextPlan(
            providers=tuple(requirements),
            memory_query=hint("memory"),
            knowledge_query=hint("knowledge"),
            graph_query=hint("graph"),
            file_query=hint("file"),
            file_ids=tuple(requested_file_ids or value("attachment_ids", ())),
            knowledge_base_ids=tuple(value("knowledge_base_ids", ())),
            max_files=self._max_files,
            limit_per_file=self._limit_per_file,
            max_items=self._max_items,
            max_tokens=self._max_tokens,
            recall_limit=self._recall_limit,
            rerank_limit=self._rerank_limit,
        )


__all__ = ["ContextPlanner"]
