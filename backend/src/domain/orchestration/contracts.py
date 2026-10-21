"""v1.1 编排所需的持久身份和消息契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from .entities import Task, TaskResult, TaskStatus


class RoundStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class OrchestrationCommandType(StrEnum):
    SUBMIT_PLAN = "SubmitPlan"
    CONTINUE_PLAN = "ContinuePlan"
    REVISE_PLAN = "RevisePlan"
    FINISH_PLAN = "FinishPlan"
    CANCEL_PLAN = "CancelPlan"


class ReviewAction(StrEnum):
    CONTINUE = "CONTINUE"
    REPLAN = "REPLAN"
    FINISH = "FINISH"


@dataclass(frozen=True, slots=True)
class PlanVersion:
    """不可变的 Plan DAG 快照；任务结果单独保存。"""

    plan_id: str
    version: int
    dag: Mapping[str, Any]
    created_at: datetime
    based_on_round_id: str | None = None
    change_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlanRound:
    """由一次 Root 审查处理的冻结任务集合。"""

    round_id: str
    plan_id: str
    plan_version: int
    round_number: int
    task_ids: tuple[str, ...]
    status: RoundStatus = RoundStatus.RUNNING
    terminal_task_count: int = 0
    created_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """带独立栅栏身份的一次 Task 执行尝试。"""

    execution_id: str
    task_id: str
    round_id: str
    attempt: int
    worker_thread_id: str
    fencing_token: int
    status: ExecutionStatus = ExecutionStatus.QUEUED
    lease_owner: str | None = None
    lease_until: datetime | None = None
    cancellation_epoch: int = 0


@dataclass(frozen=True, slots=True)
class PlanRevision:
    base_version: int
    keep_task_ids: tuple[str, ...]
    cancel_task_ids: tuple[str, ...]
    new_tasks: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    action: ReviewAction
    reason: str
    revision: PlanRevision | None = None
    final_status: str | None = None


@dataclass(frozen=True, slots=True)
class OrchestrationCommand:
    """Orchestrator 接受的幂等命令信封。"""

    message_id: str
    command_type: OrchestrationCommandType
    idempotency_key: str
    correlation_id: str
    occurred_at: datetime
    plan_id: str
    scope_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    plan_version: int | None = None
    round_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    causation_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """拒绝缺少幂等身份或使用未知协议版本的命令。"""
        if not self.message_id.strip():
            raise ValueError("message_id is required")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is required")
        if not self.plan_id.strip():
            raise ValueError("plan_id is required")
        if not self.scope_key.strip():
            raise ValueError("scope_key is required")
        if self.schema_version != 1:
            raise ValueError("unsupported orchestration command schema_version")


@dataclass(frozen=True, slots=True)
class OrchestrationEvent:
    """由 Outbox 和消费者 Inbox 共用的持久事件信封。"""

    message_id: str
    event_type: str
    correlation_id: str
    occurred_at: datetime
    plan_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    plan_version: int | None = None
    round_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    causation_id: str | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Plan 命令首次提交或幂等重放时返回的稳定回执。"""

    message_id: str
    plan_id: str
    plan_version: int
    round_id: str
    task_ids: tuple[str, ...]
    deduplicated: bool = False


@dataclass(frozen=True, slots=True)
class WorkerTaskTerminalEvent:
    """Worker 对当前 Task execution 提交的一次终态结果。"""

    message_id: str
    occurred_at: datetime
    worker_id: str
    run_id: str
    plan_id: str
    plan_version: int
    round_id: str
    task_id: str
    execution_id: str
    fencing_token: int
    cancellation_epoch: int
    status: TaskStatus
    result: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        """拒绝不完整身份、非终态结果和无效栅栏值。"""
        for name in (
            "message_id",
            "worker_id",
            "run_id",
            "plan_id",
            "round_id",
            "task_id",
            "execution_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.plan_version < 1:
            raise ValueError("plan_version must be positive")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if self.cancellation_epoch < 0:
            raise ValueError("cancellation_epoch cannot be negative")
        if self.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMED_OUT}:
            raise ValueError("worker terminal event requires completed, failed, or timed_out status")


@dataclass(frozen=True, slots=True)
class TaskEventReceipt:
    """Worker 终态事件的固定接受结果。"""

    message_id: str
    accepted: bool
    reason: str | None = None
    deduplicated: bool = False
    round_completed: bool = False
    terminal_task_count: int | None = None
    expected_task_count: int | None = None


@dataclass(frozen=True, slots=True)
class WorkerJobLease:
    """Worker Job 的持久租约和执行输入快照。"""

    job_id: str
    worker_id: str
    run_id: str
    plan_id: str
    round_id: str
    plan_version: int
    task_id: str
    execution_id: str
    session_id: str
    attempt: int
    worker_thread_id: str
    fencing_token: int
    cancellation_epoch: int
    lease_until: datetime
    task: Task
    upstream_results: Mapping[str, TaskResult] = field(default_factory=dict)


__all__ = [
    "ExecutionStatus",
    "CommandReceipt",
    "OrchestrationCommand",
    "OrchestrationCommandType",
    "OrchestrationEvent",
    "PlanRevision",
    "PlanRound",
    "PlanVersion",
    "ReviewAction",
    "ReviewDecision",
    "RoundStatus",
    "TaskAttempt",
    "TaskEventReceipt",
    "WorkerTaskTerminalEvent",
    "WorkerJobLease",
]
