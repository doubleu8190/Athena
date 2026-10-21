"""编排持久化和调度边界。"""

from __future__ import annotations

from typing import Mapping, Protocol

from .contracts import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationOutboxLease,
    RunCancellationReceipt,
    TaskEventReceipt,
    WorkerJobLease,
    WorkerTaskTerminalEvent,
)
from .entities import Plan, Task, TaskExecution, TaskResult


class OrchestrationCommandRepository(Protocol):
    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
    ) -> CommandReceipt: ...


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


class PlanRepository(Protocol):
    async def create(self, plan: Plan, tasks: list[Task]) -> Plan: ...

    async def get(self, plan_id: str) -> tuple[Plan, list[Task]] | None: ...

    async def claim_ready_tasks(
        self, plan_id: str, max_parallelism: int
    ) -> list[TaskExecution]: ...

    async def finish_task(
        self,
        task_id: str,
        execution_generation: int,
        *,
        status: str,
        output: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    async def set_task_waiting_approval(
        self,
        task_id: str,
        execution_generation: int,
        approval_batch: dict[str, object] | None = None,
    ) -> bool: ...

    async def set_task_running(self, task_id: str, execution_generation: int) -> bool: ...

    async def retry_task(self, task_id: str) -> dict[str, object] | None: ...

    async def get_results(self, plan_id: str) -> Mapping[str, TaskResult]: ...

    async def get_task_execution(self, task_id: str) -> TaskExecution | None: ...


class PlanScheduler(Protocol):
    async def start_ready_tasks(self, plan_id: str) -> dict[str, object]: ...


class WorkerExecutor(Protocol):
    async def execute(self, execution: TaskExecution) -> TaskResult: ...

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult: ...

    async def cancel(self, run_id: str) -> int: ...


class ResultSynthesizer(Protocol):
    async def synthesize(
        self,
        plan: Plan,
        results: Mapping[str, TaskResult],
    ) -> str: ...


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


__all__ = [
    "OrchestrationCommandRepository",
    "OrchestrationOutboxRepository",
    "PlanRepository",
    "PlanScheduler",
    "ResultSynthesizer",
    "RootGraphPort",
    "TaskEventRepository",
    "WorkerJobRepository",
    "WorkerExecutor",
]
