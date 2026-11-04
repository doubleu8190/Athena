"""Worker v1 命令、错误和回执的稳定应用层 JSON 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class WorkerExecutionGrantModel(BaseModel):
    """授权 Worker 读取本次执行所需资源的短期凭证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=16, max_length=8192)
    expires_at: datetime


class WorkerTaskCommandModel(BaseModel):
    """发送给 Worker 的单次执行指令。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    command_id: UUID
    run_id: str = Field(min_length=1, max_length=255)
    plan_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    operation_id: UUID
    contract_id: str = Field(min_length=1, max_length=255)
    contract_revision: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: str = Field(min_length=1, max_length=10000)
    input: dict[str, JsonValue]
    execution_grant: WorkerExecutionGrantModel


class WorkerApprovalItemModel(BaseModel):
    """单个待审批操作及其冻结参数摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: UUID
    tool_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    risk_level: Literal["low", "medium", "high", "critical"]
    arguments: dict[str, JsonValue]
    arguments_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerApprovalRequestModel(BaseModel):
    """Worker 发起的单项或批量审批请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    approval_request_id: UUID
    pause_id: UUID
    run_id: str = Field(min_length=1, max_length=255)
    plan_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    items: list[WorkerApprovalItemModel] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_items(self) -> "WorkerApprovalRequestModel":
        """确保批次中的 item_id 唯一。"""
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("worker_approval_item_id_duplicate")
        return self


class WorkerApprovalReceiptItemModel(BaseModel):
    """审批请求中单项与中心 approval_id 的稳定映射。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: UUID
    approval_id: str = Field(min_length=1, max_length=255)
    arguments_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerApprovalReceiptModel(BaseModel):
    """中心接收审批请求的幂等回执。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    approval_request_id: UUID
    pause_id: UUID
    approval_batch_id: str | None = Field(default=None, min_length=1, max_length=255)
    disposition: Literal["accepted", "duplicate"]
    status: Literal["accepted"]
    expires_at: datetime
    items: list[WorkerApprovalReceiptItemModel] = Field(min_length=1, max_length=100)
    received_at: datetime

    @model_validator(mode="after")
    def validate_receipt_items(self) -> "WorkerApprovalReceiptModel":
        """确保回执单项映射唯一且批次标识与项目数一致。"""
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("worker_approval_receipt_item_duplicate")
        if (len(self.items) == 1) != (self.approval_batch_id is None):
            raise ValueError("worker_approval_receipt_batch_identity_invalid")
        return self


class WorkerResumeDecisionModel(BaseModel):
    """单个审批项目的最终决定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: UUID
    decision: Literal["approved", "denied", "expired"]
    arguments_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decided_at: datetime


class WorkerResumeCommandModel(BaseModel):
    """发送给 Worker 的完整审批恢复命令。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    command_id: UUID
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    pause_id: UUID
    approval_request_id: UUID
    approval_id: str | None = Field(default=None, min_length=1, max_length=255)
    approval_batch_id: str | None = Field(default=None, min_length=1, max_length=255)
    decisions: list[WorkerResumeDecisionModel] = Field(min_length=1, max_length=100)
    decision_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_grant: WorkerExecutionGrantModel

    @model_validator(mode="after")
    def validate_approval_reference(self) -> "WorkerResumeCommandModel":
        """确保单项和批量恢复命令使用互斥的审批标识。"""
        if len(self.decisions) == 1:
            if not self.approval_id or self.approval_batch_id is not None:
                raise ValueError("worker_resume_single_approval_identity_invalid")
        elif self.approval_batch_id is None or self.approval_id is not None:
            raise ValueError("worker_resume_batch_approval_identity_invalid")
        if len({item.item_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("worker_resume_decision_item_duplicate")
        return self


class WorkerResumeReceiptModel(BaseModel):
    """Worker 对恢复命令的持久收件回执。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    command_id: UUID
    receipt_id: str = Field(min_length=1, max_length=255)
    disposition: Literal["accepted", "duplicate"]
    received_at: datetime


class WorkerCommandReceiptModel(BaseModel):
    """验证 Worker 对 TaskCommand 的持久收件确认。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    command_id: UUID
    receipt_id: str = Field(min_length=1, max_length=255)
    disposition: Literal["accepted", "duplicate"]
    received_at: datetime


class WorkerHttpErrorResponseModel(BaseModel):
    """Worker 协议错误响应，供 Dispatcher 按 S1 目录裁决。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=255)
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    message: str = Field(min_length=1, max_length=4000)
    directive: Literal[
        "operator_action",
        "root_replan",
        "retry_same_command",
        "retry_execution",
        "stop",
    ]
    retry_after_seconds: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
    details: dict[str, JsonValue] | None = None


class WorkerErrorModel(BaseModel):
    """Worker 返回的稳定错误类别和诊断信息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    message: str = Field(min_length=1, max_length=4000)
    retryable: bool
    details: dict[str, JsonValue] | None = None


class WorkerResultSubmissionModel(BaseModel):
    """Worker 提交的执行成功或失败结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    result_id: UUID
    generation: int = Field(ge=1)
    status: Literal["completed", "failed"]
    output: dict[str, JsonValue] | None = None
    error: WorkerErrorModel | None = None
    last_progress_sequence: int = Field(ge=0)
    display_result: str | None = Field(default=None, min_length=1, max_length=20000)

    @model_validator(mode="after")
    def validate_result_content(self) -> WorkerResultSubmissionModel:
        """确保成功和失败结果各自携带唯一对应内容。

        参数：
        - `self`（WorkerResultSubmissionModel）：已解析的结果请求。

        返回：通过状态与内容一致性校验的请求对象。

        异常：
        - ValueError：成功缺少 output、失败缺少 error 或两种内容同时出现时抛出。
        """
        if self.status == "completed" and (self.output is None or self.error is not None):
            raise ValueError("worker_success_result_content_invalid")
        if self.status == "failed" and (self.error is None or self.output is not None):
            raise ValueError("worker_failure_result_content_invalid")
        return self


class WorkerProgressSubmissionModel(BaseModel):
    """Worker 上报一条完整、不可变的用户可见进度。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """去除首尾空白，并拒绝只有空白的展示内容。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("worker_progress_content_empty")
        return normalized


class WorkerProgressReceiptModel(BaseModel):
    """返回 Progress 的稳定 Athena 身份和接收时间。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    progress_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20000)
    received_at: datetime


class WorkerResultReceiptModel(BaseModel):
    """中心对 Worker 结果提交作出的幂等裁定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    result_id: UUID
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    disposition: Literal["recorded", "retry_scheduled", "ignored"]
    reason: Literal[
        "success_recorded",
        "worker_error_retryable",
        "worker_error_final",
        "execution_attempts_exhausted",
        "output_schema_invalid",
        "task_cancelled",
        "execution_superseded",
        "execution_deadline_exceeded",
    ]
    next_execution_id: str | None = Field(default=None, min_length=1, max_length=255)
    received_at: datetime

    @model_validator(mode="after")
    def validate_retry_receipt(self) -> WorkerResultReceiptModel:
        """要求重试回执包含新 Execution ID，并禁止其他回执携带它。

        参数：
        - `self`（WorkerResultReceiptModel）：中心裁定回执。

        返回：通过处置结果一致性校验的回执对象。

        异常：
        - ValueError：retry_scheduled 与 next_execution_id 不匹配时抛出。
        """
        if (self.disposition == "retry_scheduled") != (self.next_execution_id is not None):
            raise ValueError("worker_result_receipt_retry_identity_invalid")
        return self


class WorkerCancelCommandModel(BaseModel):
    """要求 Worker 停止指定 Execution 的中心命令。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    command_id: UUID
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    reason: Literal[
        "task_cancelled",
        "run_cancelled",
        "execution_superseded",
        "execution_timed_out",
    ]

    @model_validator(mode="after")
    def validate_logical_cancel_epoch(self) -> WorkerCancelCommandModel:
        """要求逻辑取消命令携带正数取消代次。"""
        if self.reason in {"task_cancelled", "run_cancelled"} and self.cancellation_epoch < 1:
            raise ValueError("worker_cancel_command_epoch_invalid")
        return self


class WorkerCancelReceiptModel(BaseModel):
    """Worker 对取消意图持久收件的回执。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    command_id: UUID
    receipt_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    accepted: Literal[True]
    reason: Literal[
        "task_cancelled",
        "run_cancelled",
        "execution_superseded",
        "execution_timed_out",
    ]
    stop_confirmation_required: Literal[True]
    received_at: datetime

    @model_validator(mode="after")
    def validate_logical_cancel_epoch(self) -> WorkerCancelReceiptModel:
        """要求逻辑取消的代次至少为 1，与协议 Schema 保持一致。"""
        if self.reason in {"task_cancelled", "run_cancelled"} and self.cancellation_epoch < 1:
            raise ValueError("worker_cancel_receipt_epoch_invalid")
        return self


class WorkerStopConfirmationModel(BaseModel):
    """Worker 声明指定远端 Execution 已停止的稳定请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    stop_confirmation_id: UUID
    cancel_command_id: UUID
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    reason: Literal[
        "task_cancelled",
        "run_cancelled",
        "execution_superseded",
        "execution_timed_out",
    ]
    stopped: Literal[True]
    stopped_at: datetime

    @model_validator(mode="after")
    def validate_logical_cancel_epoch(self) -> WorkerStopConfirmationModel:
        """逻辑取消停止确认必须与正数取消代次绑定。"""
        if self.reason in {"task_cancelled", "run_cancelled"} and self.cancellation_epoch < 1:
            raise ValueError("worker_stop_confirmation_epoch_invalid")
        return self


class WorkerStopConfirmationReceiptModel(BaseModel):
    """持久化的停止确认裁定，可供幂等重放。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    stop_confirmation_id: UUID
    cancel_command_id: UUID
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    reason: Literal[
        "task_cancelled",
        "run_cancelled",
        "execution_superseded",
        "execution_timed_out",
    ]
    disposition: Literal["capacity_released", "already_released"]
    task_status_unchanged: Literal[True]
    received_at: datetime

    @model_validator(mode="after")
    def validate_logical_cancel_epoch(self) -> WorkerStopConfirmationReceiptModel:
        """逻辑取消回执必须与正数取消代次绑定。"""
        if self.reason in {"task_cancelled", "run_cancelled"} and self.cancellation_epoch < 1:
            raise ValueError("worker_stop_receipt_epoch_invalid")
        return self


class WorkerRegistrationResponseModel(BaseModel):
    """注册中心持久化并返回的稳定注册响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    worker_instance_id: str = Field(min_length=1, max_length=255)
    worker_instance_key: UUID
    contract_id: str = Field(min_length=1, max_length=255)
    contract_revision: int = Field(ge=1)
    status: Literal["active", "inactive"]
    registered_at: datetime


__all__ = [
    "WorkerApprovalItemModel",
    "WorkerApprovalReceiptItemModel",
    "WorkerApprovalReceiptModel",
    "WorkerApprovalRequestModel",
    "WorkerCancelCommandModel",
    "WorkerCancelReceiptModel",
    "WorkerErrorModel",
    "WorkerExecutionGrantModel",
    "WorkerResultReceiptModel",
    "WorkerResultSubmissionModel",
    "WorkerProgressReceiptModel",
    "WorkerProgressSubmissionModel",
    "WorkerResumeCommandModel",
    "WorkerResumeDecisionModel",
    "WorkerResumeReceiptModel",
    "WorkerRegistrationResponseModel",
    "WorkerStopConfirmationModel",
    "WorkerStopConfirmationReceiptModel",
    "WorkerTaskCommandModel",
    "WorkerCommandReceiptModel",
    "WorkerHttpErrorResponseModel",
]
