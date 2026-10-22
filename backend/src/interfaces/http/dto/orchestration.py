"""Run 编排快照和不可变 Plan Version 的 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class OrchestrationExecutionResponse(BaseModel):
    execution_id: str
    attempt: int
    worker_thread_id: str
    status: str
    fencing_token: int
    cancellation_epoch: int
    lease_until: datetime | None = None
    result_ref: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class OrchestrationTaskResponse(BaseModel):
    task_id: str
    title: str
    objective: str
    status: str
    active_execution_id: str | None = None
    terminal_at: datetime | None = None
    executions: list[OrchestrationExecutionResponse]


class OrchestrationRoundResponse(BaseModel):
    round_id: str
    plan_version: int
    round_number: int
    status: str
    expected_task_count: int
    terminal_task_count: int
    created_at: datetime
    completed_at: datetime | None = None
    tasks: list[OrchestrationTaskResponse]


class OrchestrationApprovalResponse(BaseModel):
    approval_id: str
    plan_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    interrupt_id: str | None = None
    tool_name: str
    risk_level: str
    status: str
    decision: str | None = None
    created_at: datetime | str
    decided_at: datetime | str | None = None


class PlanVersionSummaryResponse(BaseModel):
    version: int
    change_reason: str | None = None
    based_on_round_id: str | None = None
    created_at: datetime


class OrchestrationPlanResponse(BaseModel):
    plan_id: str
    status: str
    user_goal: str
    max_parallelism: int
    current_version: int
    active_round_id: str | None = None
    cancellation_epoch: int
    versions: list[PlanVersionSummaryResponse]
    rounds: list[OrchestrationRoundResponse]


class OrchestrationRunResponse(BaseModel):
    run_id: str
    session_id: str
    status: str
    root_thread_id: str
    cancellation_epoch: int
    cancellation_external_effect_status: str
    created_at: datetime | str
    updated_at: datetime | str


class RunOrchestrationSnapshotResponse(BaseModel):
    run: OrchestrationRunResponse
    plan: OrchestrationPlanResponse | None = None
    approvals: list[OrchestrationApprovalResponse]


class PlanVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    version: int
    dag_json: dict[str, Any]
    change_reason: str | None = None
    based_on_round_id: str | None = None
    created_at: datetime
