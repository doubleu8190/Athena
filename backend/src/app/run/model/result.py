"""Run 用例向 HTTP 边界返回的稳定结果。"""

from __future__ import annotations

from dataclasses import dataclass

from domain.run.model import (
    PendingSubagentApproval,
    PendingSubagentInput,
    Run,
    RunInputRequest,
    SubagentTaskSummary,
)
from domain.tool import PendingToolApproval


@dataclass(frozen=True, slots=True)
class RunDetailsResult:
    """Run 当前状态及可能存在的待回答请求。"""

    run: Run
    pending_input: RunInputRequest | None
    pending_approvals: tuple[PendingToolApproval, ...] = ()
    subagent_tasks: tuple[SubagentTaskSummary, ...] = ()
    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = ()
    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = ()


@dataclass(frozen=True, slots=True)
class RunListResult:
    """稳定游标分页结果。"""

    items: tuple[Run, ...]
    next_cursor: str | None


__all__ = ["RunDetailsResult", "RunListResult"]
