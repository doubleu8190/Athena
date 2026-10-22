"""与 Pydantic、数据库和 LangGraph 无关的编排实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any, Mapping


class PlanStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)
FAILED_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class PlanEdge:
    from_task_id: str
    to_task_id: str


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    title: str
    objective: str
    input: Mapping[str, Any] = field(default_factory=dict)
    expected_output: Mapping[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    worker_type: str = "general"
    timeout_seconds: float = 3600.0

    def __post_init__(self) -> None:
        """拒绝非正数或非有限值，避免 Worker 永不超时或立即失效。"""
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        try:
            timeout_seconds = float(self.timeout_seconds)
        except OverflowError as exc:
            raise ValueError(
                "timeout_seconds must be a finite positive number"
            ) from exc
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        object.__setattr__(self, "timeout_seconds", timeout_seconds)


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """一次带代际和上游结果的 Worker 执行快照。"""

    plan_id: str
    run_id: str
    session_id: str
    task: Task
    execution_generation: int
    upstream_results: Mapping[str, "TaskResult"] = field(default_factory=dict)
    execution_id: str | None = None
    worker_thread_id: str | None = None
    fencing_token: int | None = None
    cancellation_epoch: int = 0


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    goal: str
    task_ids: tuple[str, ...]
    edges: tuple[PlanEdge, ...] = ()
    max_parallelism: int = 1
    status: PlanStatus = PlanStatus.CREATED
    session_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    uncertainties: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None
    approval_batch: Mapping[str, Any] | None = None
    retryable: bool = False


__all__ = [
    "FAILED_TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "Plan",
    "PlanEdge",
    "PlanStatus",
    "Task",
    "TaskExecution",
    "TaskResult",
    "TaskStatus",
]
