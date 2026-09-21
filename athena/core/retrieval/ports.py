"""检索轨迹的基础设施端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

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
    ) -> None:
        """写入检索运行的最终统计和状态。"""
        ...

    async def mark_injected(self, run_id: str, source_ids: Sequence[str]) -> None:
        """标记最终进入上下文的候选。"""
        ...
