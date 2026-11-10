"""Run-scoped model request budget shared by answer and compression."""

from __future__ import annotations

from domain.llm.model import LlmError
from domain.run.ports import RunRepository
from domain.tool import (
    MAX_LLM_REQUESTS_PER_RUN,
    ReserveLlmRequestCommand,
    ToolExecutionContext,
)


class RunLlmBudget:
    """Atomically reserve provider calls without ever refunding a failed attempt."""

    def __init__(
        self,
        repository: RunRepository,
        context: ToolExecutionContext,
        *,
        used: int = 0,
        maximum: int = MAX_LLM_REQUESTS_PER_RUN,
        worker_id: str,
    ) -> None:
        if not 0 <= used <= maximum or not 1 <= maximum <= MAX_LLM_REQUESTS_PER_RUN:
            raise ValueError("run_llm_request_limit_invalid")
        self._repository = repository
        self._context = context
        self._maximum = maximum
        self._worker_id = worker_id
        self.used = used

    @property
    def remaining(self) -> int:
        return self._maximum - self.used

    @property
    def maximum(self) -> int:
        return self._maximum

    @property
    def can_compress(self) -> bool:
        """Keep one request available for the mandatory final-answer attempt."""
        # 始终为最后一次“无工具最终回答”保留一个预算槽位。
        return self.used < self._maximum - 1

    @property
    def final_only(self) -> bool:
        # 只剩最后一个槽位时，下一轮必须直接产出最终回答。
        return self.used >= self._maximum - 1

    async def reserve(self) -> int:
        """Consume one slot and return its 1-based request number."""
        # 先做本地检查，再由仓储原子递增数据库计数器，保证跨进程不超发。
        if self.remaining <= 0:
            raise LlmError("run_llm_request_limit_exceeded")
        sequence = await self._repository.reserve_llm_request(
            ReserveLlmRequestCommand(
                run_id=self._context.run_id,
                worker_id=self._worker_id,
                execution_epoch=self._context.execution_epoch,
                maximum_count=self._maximum,
            )
        )
        if sequence is None:
            raise LlmError("run_execution_lease_lost")
        # 数据库返回的序号必须单调且不超上限，否则说明计数状态不一致。
        if sequence <= self.used or sequence > self._maximum:
            raise LlmError("run_llm_request_counter_invalid")
        self.used = sequence
        return sequence


__all__ = ["RunLlmBudget"]
