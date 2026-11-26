"""Run 级进程内模型请求预算计数器，供主回答与上下文压缩共享。"""

from __future__ import annotations

from domain.llm.model import LlmError
from domain.subagent import MAX_SUBAGENT_MAX_LLM_REQUESTS
from domain.tool import MAX_LLM_REQUESTS_PER_RUN

# 主 Run 默认上限为 40，子 Run 与数据库 CHECK 约束允许最高 50，预算上限与之保持一致。
_MAX_ALLOWED_LLM_REQUESTS = max(MAX_LLM_REQUESTS_PER_RUN, MAX_SUBAGENT_MAX_LLM_REQUESTS)


class RunLlmBudget:
    """单租约进程内计数器：原子预留槽位且失败不退还，随状态命令持久化至 runs.llm_request_count。"""

    def __init__(
        self,
        *,
        used: int = 0,
        maximum: int = MAX_LLM_REQUESTS_PER_RUN,
    ) -> None:
        if not 0 <= used <= maximum or not 1 <= maximum <= _MAX_ALLOWED_LLM_REQUESTS:
            raise ValueError("run_llm_request_limit_invalid")
        self._maximum = maximum
        self.used = used

    @property
    def remaining(self) -> int:
        return self._maximum - self.used

    @property
    def maximum(self) -> int:
        return self._maximum

    @property
    def can_compress(self) -> bool:
        """始终为最后一次无工具最终回答保留一个预算槽位。"""
        return self.used < self._maximum - 1

    @property
    def final_only(self) -> bool:
        """只剩最后一个槽位时，下一轮必须直接产出最终回答。"""
        return self.used >= self._maximum - 1

    def reserve(self) -> int:
        """在进程内消耗一个槽位并返回 1-based 请求序号；失败尝试绝不退还。"""
        if self.remaining <= 0:
            raise LlmError("run_llm_request_limit_exceeded")
        self.used += 1
        return self.used


__all__ = ["RunLlmBudget"]
