"""远程 Worker 注册与逐轮编排所需的纯领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


class WorkerIdempotencyConflict(ValueError):
    """幂等标识已被不同请求内容占用。"""


class WorkerInstanceStatus(StrEnum):
    """Worker 实例的健康状态。"""

    READY = "ready"
    NOT_READY = "not_ready"
    DISABLED = "disabled"


class WorkerExecutionStatus(StrEnum):
    """一次远程 Worker 执行尝试的状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class WorkerCapabilityRevision:
    """描述一个不可变的 Worker 能力契约版本。"""

    contract_id: str
    revision: int
    can_do: tuple[str, ...]
    cannot_do: tuple[str, ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]

    def __post_init__(self) -> None:
        """校验契约标识、版本和四项能力说明。

        参数：无。

        返回：无。

        异常：
        - ValueError：标识、版本或能力说明不符合注册契约时抛出。
        """
        if not self.contract_id.strip() or self.revision < 1:
            raise ValueError("worker_contract_identity_invalid")
        if not self.can_do or not self.cannot_do:
            raise ValueError("worker_capability_description_required")
        if any(not item.strip() for item in (*self.can_do, *self.cannot_do)):
            raise ValueError("worker_capability_description_empty")
        if self.input_schema.get("type") != "object":
            raise ValueError("worker_input_schema_must_be_object")
        if self.output_schema.get("type") != "object":
            raise ValueError("worker_output_schema_must_be_object")


@dataclass(frozen=True, slots=True)
class WorkerRegistrationIntent:
    """描述已通过身份认证、待持久化的 Worker 注册请求。"""

    registration_request_id: str
    service_principal: str
    instance_key: str
    base_url: str
    execution_timeout_seconds: int
    max_concurrency: int
    can_do: tuple[str, ...]
    cannot_do: tuple[str, ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    request_hash: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerRegistrationReceipt:
    """返回稳定的 Worker 注册身份及绑定能力版本。"""

    instance_id: str
    instance_key: str
    contract_id: str
    contract_revision: int
    registered_at: datetime
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class WorkerInstance:
    """描述一个提供指定能力版本的远程 Worker 服务实例。"""

    instance_id: str
    instance_key: str
    contract_id: str
    contract_revision: int
    base_url: str
    execution_timeout_seconds: int
    max_concurrency: int
    status: WorkerInstanceStatus = WorkerInstanceStatus.NOT_READY
    accepting_tasks: bool = False

    def __post_init__(self) -> None:
        """拒绝无法安全调度的实例配置。

        参数：无。

        返回：无。

        异常：
        - ValueError：实例身份、地址、超时或并发范围不合法时抛出。
        """
        if not all(
            value.strip()
            for value in (
                self.instance_id,
                self.instance_key,
                self.contract_id,
                self.base_url,
            )
        ):
            raise ValueError("worker_instance_identity_invalid")
        if self.contract_revision < 1:
            raise ValueError("worker_contract_revision_invalid")
        if not 1 <= self.execution_timeout_seconds <= 604800:
            raise ValueError("worker_execution_timeout_out_of_range")
        if not 1 <= self.max_concurrency <= 256:
            raise ValueError("worker_max_concurrency_out_of_range")
        if self.status is WorkerInstanceStatus.NOT_READY and self.accepting_tasks:
            raise ValueError("worker_not_ready_cannot_accept_tasks")


class WorkerTaskStatus(StrEnum):
    """逻辑任务状态；自动重试期间任务保持非终态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATUSES = frozenset(
    {WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED, WorkerTaskStatus.CANCELLED}
)


class WorkerPlanStatus(StrEnum):
    """一次 Run 内单轮 Plan 的状态。"""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkerTaskSpec:
    """描述单轮 Plan 中不依赖其他任务的一个逻辑任务。"""

    task_ref: str
    contract_id: str
    contract_revision: int
    objective: str
    input: dict[str, JsonValue]
    acceptance_criteria: str

    def __post_init__(self) -> None:
        """校验任务身份和能力引用。

        参数：无。

        返回：无。

        异常：
        - ValueError：任务引用、能力引用或任务目标为空/非法时抛出。
        """
        if not all(
            value.strip()
            for value in (
                self.task_ref,
                self.contract_id,
                self.objective,
                self.acceptance_criteria,
            )
        ):
            raise ValueError("worker_task_required_field_empty")
        if self.contract_revision < 1:
            raise ValueError("worker_contract_revision_invalid")


@dataclass(frozen=True, slots=True)
class WorkerPlanSubmission:
    """描述要写入 Run 的一轮不可变 Plan。"""

    run_id: str
    plan_id: str
    sequence: int
    idempotency_key: str
    goal: str
    tasks: tuple[WorkerTaskSpec, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        """确保一轮 Plan 有唯一序号、幂等标识和至少一个任务。

        参数：无。

        返回：无。

        异常：
        - ValueError：Plan 字段缺失、序号无效或任务引用重复时抛出。
        """
        if not self.run_id.strip() or not self.plan_id.strip():
            raise ValueError("worker_plan_identity_required")
        if not self.idempotency_key.strip() or not self.goal.strip():
            raise ValueError("worker_plan_idempotency_and_goal_required")
        if self.sequence < 1 or not self.tasks:
            raise ValueError("worker_plan_sequence_and_tasks_required")
        task_refs = tuple(task.task_ref for task in self.tasks)
        if len(set(task_refs)) != len(task_refs):
            raise ValueError("worker_plan_task_ref_duplicate")


@dataclass(frozen=True, slots=True)
class WorkerExecutionDeadline:
    """首次发送时冻结的执行开始时间和截止时间。"""

    started_at: datetime
    deadline_at: datetime
    timeout_seconds: float

    def __post_init__(self) -> None:
        """验证执行截止时间为正且有限。

        参数：无。

        返回：无。

        异常：
        - ValueError：时长非正数、非有限值或截止早于开始时抛出。
        """
        if (
            not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.deadline_at <= self.started_at
        ):
            raise ValueError("worker_execution_deadline_invalid")


@dataclass(frozen=True, slots=True)
class WorkerPlanReceipt:
    """返回已持久化 Plan 的稳定提交回执。"""

    plan_id: str
    run_id: str
    sequence: int
    accepted: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class WorkerDispatchLease:
    """描述命令投递所有权及其持久化执行和实例内容。"""

    command_id: str
    execution_id: str
    lease_owner: str
    lease_token: str
    attempt: int
    command: dict[str, JsonValue]
    instance_id: str | None = None
    instance_key: str | None = None
    service_principal: str | None = None
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerDispatchStart:
    """描述已分配执行及可选的首发前 deadline 快照。"""

    command_id: str
    execution_id: str
    instance_id: str
    instance_key: str
    service_principal: str
    base_url: str
    command: dict[str, JsonValue]
    request_hash: str
    started_at: datetime | None
    deadline_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkerResultReceipt:
    """返回 Worker 结果首次裁定或幂等重放的持久回执。"""

    receipt_id: str
    execution_id: str
    result_id: str
    disposition: str
    reason: str
    generation: int
    received_at: datetime
    next_execution_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class WorkerExecutionError:
    """描述 Worker 返回的稳定执行错误。"""

    code: str
    message: str
    retryable: bool
    details: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class WorkerResultSubmission:
    """描述单次执行的完整、可幂等重放结果。"""

    execution_id: str
    instance_id: str
    generation: int
    result_id: str
    status: str
    output: dict[str, JsonValue] | None = None
    error: WorkerExecutionError | None = None
    request_hash: str | None = None

    def __post_init__(self) -> None:
        """校验结果请求的身份和成功/失败内容互斥关系。

        参数：无。

        返回：无。

        异常：
        - ValueError：身份缺失、代次非法或结果内容与状态不匹配时抛出。
        """
        if not all(
            value.strip()
            for value in (
                self.execution_id,
                self.instance_id,
                self.result_id,
            )
        ):
            raise ValueError("worker_result_identity_required")
        if self.generation < 1:
            raise ValueError("worker_result_generation_invalid")
        if self.status == "completed":
            if self.output is None or self.error is not None:
                raise ValueError("worker_success_result_content_invalid")
        elif self.status == "failed":
            if self.error is None or self.output is not None:
                raise ValueError("worker_failure_result_content_invalid")
        else:
            raise ValueError("worker_result_status_invalid")


@dataclass(frozen=True, slots=True)
class WorkerStopConfirmation:
    """描述 Worker 确认某个旧 Execution 已不会继续运行的事实。"""

    instance_id: str
    stop_confirmation_id: str
    cancel_command_id: str
    execution_id: str
    generation: int
    cancellation_epoch: int
    reason: str
    stopped_at: datetime
    request_hash: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerStopReceipt:
    """返回持久停止确认及其容量释放裁定。"""

    stop_confirmation_id: str
    cancel_command_id: str
    execution_id: str
    generation: int
    cancellation_epoch: int
    reason: str
    disposition: str
    received_at: datetime
    task_status_unchanged: bool = True
    replayed: bool = False


__all__ = [
    "JsonValue",
    "TASK_TERMINAL_STATUSES",
    "WorkerCapabilityRevision",
    "WorkerExecutionDeadline",
    "WorkerExecutionError",
    "WorkerExecutionStatus",
    "WorkerDispatchLease",
    "WorkerDispatchStart",
    "WorkerInstance",
    "WorkerInstanceStatus",
    "WorkerPlanStatus",
    "WorkerRegistrationIntent",
    "WorkerRegistrationReceipt",
    "WorkerPlanSubmission",
    "WorkerPlanReceipt",
    "WorkerResultReceipt",
    "WorkerResultSubmission",
    "WorkerStopConfirmation",
    "WorkerStopReceipt",
    "WorkerTaskSpec",
    "WorkerTaskStatus",
]
