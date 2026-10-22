"""编排持久化和调度边界。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .contracts import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationOutboxLease,
    ReviewDecision,
    ReviewContext,
    RunCancellationReceipt,
    TaskEventReceipt,
    WorkerJobLease,
    WorkerTaskTerminalEvent,
)
from .entities import TaskExecution, TaskResult


class OrchestrationCommandRepository(Protocol):
    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
    ) -> CommandReceipt: ...

    async def apply_review_decision(
        self,
        command: OrchestrationCommand,
        decision: ReviewDecision,
    ) -> CommandReceipt: ...

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None: ...


class TaskEventRepository(Protocol):
    async def apply_worker_terminal_event(
        self, event: WorkerTaskTerminalEvent
    ) -> TaskEventReceipt: ...


class WorkerJobRepository(Protocol):
    async def claim_next(
        self, worker_id: str, *, lease_seconds: float
    ) -> WorkerJobLease | None: ...

    async def renew_lease(
        self, lease: WorkerJobLease, *, lease_seconds: float
    ) -> WorkerJobLease | None: ...

    async def wait_for_approval(
        self,
        lease: WorkerJobLease,
        approval: Mapping[str, object],
    ) -> bool: ...

    async def acknowledge_cancelled(self, lease: WorkerJobLease) -> bool: ...

    async def reconcile_cancellations(self) -> list[RunCancellationReceipt]: ...


class OrchestrationOutboxRepository(Protocol):
    """用户可见编排事件的可靠领取和至少一次投递状态。"""

    async def claim_next_event(
        self, worker_id: str, *, lease_seconds: float
    ) -> OrchestrationOutboxLease | None: ...

    async def mark_event_published(
        self, lease: OrchestrationOutboxLease
    ) -> bool: ...

    async def retry_event(
        self, lease: OrchestrationOutboxLease, *, delay_seconds: float
    ) -> bool: ...


class ReviewContextRepository(Protocol):
    async def load_review_context(self, round_id: str) -> ReviewContext: ...


class WorkerExecutor(Protocol):
    async def execute(self, execution: TaskExecution) -> TaskResult: ...

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult: ...

    async def pending_interrupt(
        self, execution: TaskExecution
    ) -> dict[str, Any] | None: ...

class RootGraphPort(Protocol):

    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        message_id: str = "",
        attachment_ids: list[str] | None = None,
        resume_value: dict[str, object] | None = None,
        stop_signal: object | None = None,
    ) -> object: ...

    async def has_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool: ...

    async def find_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
    ) -> tuple[str, str] | None:
        """按 Run 和 Round 查找中断 ID 与 checkpoint ID。"""
        ...


__all__ = [
    "OrchestrationCommandRepository",
    "OrchestrationOutboxRepository",
    "ReviewContextRepository",
    "RootGraphPort",
    "TaskEventRepository",
    "WorkerJobRepository",
    "WorkerExecutor",
]
