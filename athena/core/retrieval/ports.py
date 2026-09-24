"""检索轨迹的基础设施端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .contracts import RetrievalCandidate, RetrievalRunRequest


class RetrievalTraceWriter(Protocol):
    """记录检索运行和候选轨迹的最小端口。"""

    async def start_run(self, request: RetrievalRunRequest) -> str:
        """创建一次检索运行并返回稳定的运行 ID。"""
        ...

    async def record_candidates(
        self, run_id: str, candidates: Sequence[RetrievalCandidate]
    ) -> None:
        """追加某一阶段的候选轨迹。"""
        ...

    async def complete_run(
        self,
        run_id: str,
        *,
        candidate_count: int,
        selected_count: int = 0,
        injected_count: int = 0,
        status: str = "succeeded",
        duration_ms: float | None = None,
        error_message: str | None = None,
    ) -> None:
        """写入检索运行的最终统计和状态。"""
        ...

    async def mark_injected(self, run_id: str, source_ids: Sequence[str]) -> None:
        """标记最终进入上下文的候选。"""
        ...


class RetrievalTraceReader(Protocol):
    """检索轨迹查询端口，供 API 用例依赖而不暴露数据库实现。"""

    async def list_runs(
        self,
        *,
        limit: int = 30,
        offset: int = 0,
        scope: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页读取检索运行摘要。"""
        ...

    async def get_run_with_candidates(
        self, run_id: str
    ) -> dict[str, Any] | None:
        """读取运行摘要及其候选；运行不存在时返回 None。"""
        ...
