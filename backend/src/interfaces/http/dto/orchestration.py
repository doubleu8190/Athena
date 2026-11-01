"""Run 编排快照和不可变 Plan Version 的 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class OrchestrationRunResponse(BaseModel):
    run_id: str
    session_id: str
    status: str
    root_thread_id: str
    cancellation_epoch: int
    cancellation_external_effect_status: str
    created_at: datetime
    updated_at: datetime


class WorkerApprovalItemSnapshotResponse(BaseModel):
    """S6 审批批次中的单项事实。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    approval_id: str
    tool_name: str
    summary: str
    risk_level: str
    arguments: dict[str, JsonValue]
    arguments_sha256: str
    status: str
    decision: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


class WorkerApprovalSnapshotResponse(BaseModel):
    """按 Worker ApprovalRequest 聚合的展示快照。"""

    model_config = ConfigDict(extra="forbid")

    approval_request_id: str
    pause_id: str
    approval_batch_id: str | None = None
    run_id: str
    plan_id: str
    task_id: str
    execution_id: str
    generation: int
    # request.status 描述审批批次的中心生命周期，区别于 item.status。
    status: str
    expires_at: datetime
    received_at: datetime
    items: list[WorkerApprovalItemSnapshotResponse] = Field(min_length=1)


class RunOrchestrationSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: OrchestrationRunResponse
    worker_plans: list[WorkerPlanSnapshotResponse] = Field(default_factory=list)
    approvals: list[WorkerApprovalSnapshotResponse] = Field(default_factory=list)


class WorkerExecutionSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    generation: int
    status: str
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    cancellation_epoch: int
    capacity_reserved: bool
    error: dict[str, JsonValue] | None = None


class WorkerTaskSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    operation_id: str
    task_ref: str
    objective: str
    acceptance_criteria: str
    status: str
    active_execution_id: str | None = None
    generation: int
    output: JsonValue | None = None
    error: JsonValue | None = None
    executions: list[WorkerExecutionSnapshotResponse]


class WorkerPlanSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    run_id: str
    sequence: int
    goal: str
    status: str
    expected_task_count: int
    terminal_task_count: int
    root_wait_generation: int
    created_at: datetime
    completed_at: datetime | None = None
    tasks: list[WorkerTaskSnapshotResponse]
