"""v1.1 编排所需的持久身份和消息契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from .entities import PlanStatus, Task, TaskResult, TaskStatus


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
class ReviewContext:
    """Root 基于固定 Plan 版本和 Round 事实进行审查的只读上下文。"""

    original_goal: str
    plan_id: str
    plan_version: int
    round_id: str
    full_dag_summary: Mapping[str, Any]
    previous_results_summary: tuple[Mapping[str, Any], ...] = ()
    round_results: tuple[Mapping[str, Any], ...] = ()
    remaining_tasks: tuple[Mapping[str, Any], ...] = ()
    user_constraints: tuple[str, ...] = ()
    remaining_budget: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝缺失身份或未结构化的 Review 输入。"""
        if not isinstance(self.original_goal, str) or not self.original_goal.strip():
            raise ValueError("original_goal is required")
        if (
            not isinstance(self.plan_id, str)
            or not self.plan_id.strip()
            or not isinstance(self.round_id, str)
            or not self.round_id.strip()
        ):
            raise ValueError("plan_id and round_id are required")
        if (
            not isinstance(self.plan_version, int)
            or isinstance(self.plan_version, bool)
            or self.plan_version < 1
        ):
            raise ValueError("plan_version must be positive")
        if not isinstance(self.full_dag_summary, Mapping):
            raise ValueError("full_dag_summary must be a mapping")
        for name, items in (
            ("previous_results_summary", self.previous_results_summary),
            ("round_results", self.round_results),
            ("remaining_tasks", self.remaining_tasks),
        ):
            if any(not isinstance(item, Mapping) for item in items):
                raise ValueError(f"{name} must contain mappings")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.user_constraints
        ):
            raise ValueError("user_constraints must contain non-empty strings")
        if not isinstance(self.remaining_budget, Mapping):
            raise ValueError("remaining_budget must be a mapping")


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

    def __post_init__(self) -> None:
        """校验修订中的任务身份集合，避免重复或互相冲突。"""
        if self.base_version < 1:
            raise ValueError("base_version must be positive")
        for name, task_ids in (
            ("keep_task_ids", self.keep_task_ids),
            ("cancel_task_ids", self.cancel_task_ids),
        ):
            if any(
                not isinstance(task_id, str) or not task_id.strip()
                for task_id in task_ids
            ):
                raise ValueError(f"{name} must contain non-empty task IDs")
            if len(task_ids) != len(set(task_ids)):
                raise ValueError(f"{name} cannot contain duplicate task IDs")
        if set(self.keep_task_ids) & set(self.cancel_task_ids):
            raise ValueError("kept and cancelled task IDs must be disjoint")

        new_task_ids: list[str] = []
        for task in self.new_tasks:
            if not isinstance(task, Mapping):
                raise ValueError("new_tasks must contain task objects")
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("every new task requires a non-empty task_id")
            new_task_ids.append(task_id)
        if len(new_task_ids) != len(set(new_task_ids)):
            raise ValueError("new task IDs cannot contain duplicates")
        if set(new_task_ids) & (set(self.keep_task_ids) | set(self.cancel_task_ids)):
            raise ValueError("new task IDs cannot reuse existing task IDs")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    action: ReviewAction
    reason: str
    revision: PlanRevision | None = None
    final_status: str | None = None

    def __post_init__(self) -> None:
        """保证 Review 决策只符合一种合法动作的字段组合。"""
        if not isinstance(self.action, ReviewAction):
            raise ValueError("action must be a ReviewAction")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason is required")
        if self.action == ReviewAction.CONTINUE:
            if self.revision is not None or self.final_status is not None:
                raise ValueError("CONTINUE cannot include revision or final_status")
        elif self.action == ReviewAction.REPLAN:
            if (
                not isinstance(self.revision, PlanRevision)
                or self.final_status is not None
            ):
                raise ValueError(
                    "REPLAN requires revision and cannot include final_status"
                )
        elif self.action == ReviewAction.FINISH:
            if self.revision is not None:
                raise ValueError("FINISH cannot include revision")
            if not isinstance(self.final_status, str) or self.final_status not in {
                PlanStatus.COMPLETED.value,
                PlanStatus.FAILED.value,
            }:
                raise ValueError("FINISH final_status must be completed or failed")


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
        if self.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise ValueError("worker terminal event requires completed or failed status")


@dataclass(frozen=True, slots=True)
class TaskEventReceipt:
    """Worker 终态事件的固定接受结果。"""

    message_id: str
    accepted: bool
    reason: str | None = None
    deduplicated: bool = False
    round_completed: bool = False
    retry_scheduled: bool = False
    next_execution_id: str | None = None
    terminal_task_count: int | None = None
    expected_task_count: int | None = None


@dataclass(frozen=True, slots=True)
class RunCancellationReceipt:
    """一次 Run 取消已收敛且可发布终止事件的持久回执。"""

    run_id: str
    session_id: str
    plan_id: str
    cancellation_epoch: int
    external_effect_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class OrchestrationOutboxLease:
    """已被 Dispatcher 独占领取的用户可见 Orchestration Outbox 事件。"""

    message_id: str
    worker_id: str
    event_type: str
    run_id: str
    plan_id: str | None
    session_id: str
    plan_version: int | None
    round_id: str | None
    occurred_at: datetime
    payload: Mapping[str, Any]
    attempt: int
    lease_until: datetime


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
    job_type: str = "execute"
    approval_id: str | None = None
    resume_value: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "ExecutionStatus",
    "CommandReceipt",
    "OrchestrationCommand",
    "OrchestrationCommandType",
    "OrchestrationEvent",
    "PlanRevision",
    "PlanRound",
    "PlanVersion",
    "ReviewContext",
    "ReviewAction",
    "ReviewDecision",
    "RunCancellationReceipt",
    "OrchestrationOutboxLease",
    "RoundStatus",
    "TaskAttempt",
    "TaskEventReceipt",
    "WorkerTaskTerminalEvent",
    "WorkerJobLease",
]
