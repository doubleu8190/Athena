"""顶层 Agent、Worker 和 Coordinator 共享的编排契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictContract(BaseModel):
    """拒绝未知字段的版本化内部契约。"""

    model_config = ConfigDict(extra="forbid")


class AgentRole(StrEnum):
    """运行时赋予 Harness 的可信角色。"""

    ROOT = "root"
    PLANNER = "planner"
    WORKER = "worker"
    SYNTHESIZER = "synthesizer"


class PlanStatus(StrEnum):
    """执行计划的持久化生命周期。"""

    PLANNING = "planning"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """子任务的持久化生命周期。"""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_WAIT = "retry_wait"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INVALID_OUTPUT = "invalid_output"
    CANCELLED = "cancelled"


class WorkerResultStatus(StrEnum):
    """一次 Worker 尝试的标准结果状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.TIMED_OUT,
        TaskStatus.INVALID_OUTPUT,
        TaskStatus.CANCELLED,
    }
)

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.CLAIMED, TaskStatus.CANCELLED}),
    TaskStatus.CLAIMED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.CANCEL_REQUESTED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.RETRY_WAIT,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.INVALID_OUTPUT,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCEL_REQUESTED}
    ),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.CANCEL_REQUESTED: frozenset({TaskStatus.CANCELLED}),
    **{status: frozenset() for status in TERMINAL_TASK_STATUSES},
}


def ensure_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    """校验一次任务状态转换是否合法。

    参数：
        current: 当前持久化状态。
        target: 准备写入的下一状态。

    返回值：
        None: 转换合法。

    异常：
        ValueError: 目标状态不在当前状态允许的后继集合中。
    """

    if target not in ALLOWED_TASK_TRANSITIONS[current]:
        raise ValueError(f"invalid task transition: {current.value} -> {target.value}")


class TaskSpec(StrictContract):
    """顶层 Agent 分配给单个 Worker 的完整任务。"""

    task_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    input_context: str = ""
    expected_output: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, max_length=0)
    max_turns: int = Field(default=5, ge=1, le=20)
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    retry_limit: int = Field(default=1, ge=0, le=3)


class ExecutionPlan(StrictContract):
    """顶层 Agent 提交并经校验的 V1 执行计划。"""

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    root_run_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=6)
    max_parallelism: int = Field(default=4, ge=1, le=6)

    @model_validator(mode="after")
    def validate_v1_tasks(self) -> ExecutionPlan:
        """校验任务标识、计划归属和 V1 无依赖约束。

        返回值：
            ExecutionPlan: 校验通过的当前计划。

        异常：
            ValueError: task ID 重复、归属错误或包含依赖。
        """

        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_id must be unique within a plan")
        if any(task.plan_id != self.plan_id for task in self.tasks):
            raise ValueError("every task must belong to the execution plan")
        if any(task.depends_on for task in self.tasks):
            raise ValueError("V1 execution plans do not support task dependencies")
        return self


class TaskDraft(StrictContract):
    """顶层 Agent 决策中的任务草稿，尚未绑定 Root Run。"""

    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    input_context: str = ""
    expected_output: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=5, ge=1, le=20)
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    retry_limit: int = Field(default=1, ge=0, le=3)


class PlanSubmission(StrictContract):
    """顶层 Agent LLM 提交给运行时的执行计划。"""

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    goal: str = ""
    tasks: list[TaskDraft] = Field(min_length=1, max_length=6)
    max_parallelism: int = Field(default=4, ge=1, le=6)


PLAN_SUBMISSION_TOOL_NAME = "submit_plan"


class WorkerResult(StrictContract):
    """Worker 使用 Structured Output 返回的一次尝试结果。"""

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: WorkerResultStatus
    output: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> WorkerResult:
        """保证成功结果有输出、失败结果有错误说明。

        返回值：
            WorkerResult: 校验通过的当前结果。

        异常：
            ValueError: 结果内容与状态不一致。
        """

        if self.status == WorkerResultStatus.COMPLETED and not self.output:
            raise ValueError("completed worker result must contain output")
        if self.status != WorkerResultStatus.COMPLETED and not (
            self.error_code or self.error_message
        ):
            raise ValueError("non-completed worker result must describe the error")
        return self
