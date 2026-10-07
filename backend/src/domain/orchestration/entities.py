"""与 Pydantic、数据库和 LangGraph 无关的编排实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class PlanStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    DONE = "done"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.CANCELLED}
)
FAILED_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.CANCELLED}
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


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """一次带代际和上游结果的 Worker 执行快照。"""

    plan_id: str
    run_id: str
    session_id: str
    task: Task
    execution_generation: int
    upstream_results: Mapping[str, "TaskResult"] = field(default_factory=dict)


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
