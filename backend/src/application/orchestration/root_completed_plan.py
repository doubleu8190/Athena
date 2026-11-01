"""Root checkpoint 中已完成 Worker Plan 的稳定 JSON 内容。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue


class RootExecutionAttemptFactModel(BaseModel):
    """一次 Worker 执行尝试的终态与时间事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int
    status: str
    error: JsonValue | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None


class RootPlanTaskFactModel(BaseModel):
    """一个逻辑 Task 的最终结果、产物与执行尝试。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str
    status: str
    output: JsonValue | None = None
    error: JsonValue | None = None
    resource_ids: tuple[str, ...] = ()
    attempts: tuple[RootExecutionAttemptFactModel, ...] = ()
    objective: str = ""
    acceptance_criteria: str = ""


class RootCompletedPlanModel(BaseModel):
    """一轮 Plan 的完整结果快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    plan_id: str
    sequence: int
    root_wait_generation: int
    goal: str
    status: str
    tasks: tuple[RootPlanTaskFactModel, ...]


__all__ = [
    "RootCompletedPlanModel",
    "RootExecutionAttemptFactModel",
    "RootPlanTaskFactModel",
]
