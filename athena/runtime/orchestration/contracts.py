"""Backward-compatible exports for orchestration contracts."""

from athena.contracts.orchestration import (
    ALLOWED_TASK_TRANSITIONS,
    PLAN_SUBMISSION_TOOL_NAME,
    TERMINAL_TASK_STATUSES,
    AgentRole,
    ExecutionPlan,
    PlanStatus,
    PlanSubmission,
    StrictContract,
    TaskDraft,
    TaskSpec,
    TaskStatus,
    WorkerResult,
    WorkerResultStatus,
    ensure_task_transition,
)

__all__ = [
    "ALLOWED_TASK_TRANSITIONS",
    "PLAN_SUBMISSION_TOOL_NAME",
    "TERMINAL_TASK_STATUSES",
    "AgentRole",
    "ExecutionPlan",
    "PlanStatus",
    "PlanSubmission",
    "StrictContract",
    "TaskDraft",
    "TaskSpec",
    "TaskStatus",
    "WorkerResult",
    "WorkerResultStatus",
    "ensure_task_transition",
]
