"""编排持久化和调度边界。"""

from __future__ import annotations

from typing import Mapping, Protocol

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
from .worker_execution_messages import (
    WorkerExecutionMessage,
    WorkerExecutionMessageAppend,
)
from .entities import Plan, Task
from .worker_entities import (
    WorkerPlanReceipt,
    WorkerPlanSubmission,
    WorkerRegistrationIntent,
    WorkerRegistrationReceipt,
    WorkerResultReceipt,
    WorkerResultSubmission,
    WorkerStopConfirmation,
    WorkerStopReceipt,
)


class WorkerOrchestrationRepository(Protocol):
    """逐轮 Worker Plan、结果和命令的事务持久化边界。"""

    async def find_registration_receipt(
        self,
        service_principal: str,
        registration_request_id: str,
        request_hash: str,
    ) -> WorkerRegistrationReceipt | None: ...

    async def register_worker(
        self, intent: WorkerRegistrationIntent
    ) -> WorkerRegistrationReceipt: ...

    async def submit_plan(
        self, submission: WorkerPlanSubmission
    ) -> WorkerPlanReceipt: ...

    async def record_result(
        self,
        *,
        submission: WorkerResultSubmission,
    ) -> WorkerResultReceipt: ...

    async def confirm_stopped(
        self, confirmation: WorkerStopConfirmation
    ) -> WorkerStopReceipt: ...


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


class WorkerExecutionMessageRepository(Protocol):
    """Worker 模型交互和工具结果的有序持久化能力。"""

    async def append(
        self, message: WorkerExecutionMessageAppend
    ) -> WorkerExecutionMessage: ...

    async def list_by_execution(
        self, task_id: str, execution_generation: int
    ) -> list[WorkerExecutionMessage]: ...


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


__all__ = [
    "OrchestrationCommandRepository",
    "OrchestrationOutboxRepository",
    "ReviewContextRepository",
    "TaskEventRepository",
    "WorkerJobRepository",
    "WorkerExecutionMessageRepository",
    "WorkerOrchestrationRepository",
]
