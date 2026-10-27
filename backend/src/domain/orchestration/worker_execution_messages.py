"""Worker 模型交互和工具回执的持久消息契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from domain.leases import LeaseGrant


class WorkerExecutionEntryType(StrEnum):
    """Worker 执行轨迹中的记录种类。"""

    LLM_MESSAGE = "llm_message"
    TOOL_RESULT = "tool_result"


class WorkerLLMRole(StrEnum):
    """Worker 实际发送或接收的模型消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class WorkerToolCallContent:
    """Worker assistant 回复中的一个规范化工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        """确保工具调用具备稳定身份、名称和对象参数。"""
        if not self.id.strip() or not self.name.strip():
            raise ValueError("worker_tool_call_identity_required")
        if not isinstance(self.arguments, dict):
            raise ValueError("worker_tool_call_arguments_invalid")


@dataclass(frozen=True, slots=True)
class WorkerToolResultContent:
    """Worker 工具回执中模型可见的结果部分。"""

    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: int | None = None
    result_ref: str | None = None
    truncated: bool | None = None
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """确保工具结果的状态和可选耗时有效。"""
        if not self.status.strip():
            raise ValueError("worker_tool_result_status_required")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("worker_tool_result_duration_invalid")
        if not isinstance(self.extra_fields, dict):
            raise ValueError("worker_tool_result_extra_fields_invalid")


@dataclass(frozen=True, slots=True)
class WorkerExecutionMessageContent:
    """Worker 消息正文及其结构化工具信息。"""

    content: Any = None
    tool_calls: tuple[WorkerToolCallContent, ...] = ()
    tool_result: WorkerToolResultContent | None = None

    def __post_init__(self) -> None:
        """确保工具调用和回执均已转换成领域类型。"""
        if any(not isinstance(call, WorkerToolCallContent) for call in self.tool_calls):
            raise ValueError("worker_execution_tool_calls_invalid")
        if self.tool_result is not None and not isinstance(
            self.tool_result, WorkerToolResultContent
        ):
            raise ValueError("worker_execution_tool_result_invalid")


@dataclass(frozen=True, slots=True)
class WorkerExecutionMessageAppend:
    """等待仓储追加的 Worker 模型消息或工具结果记录。"""

    message_id: str
    session_id: str
    run_id: str
    plan_id: str
    task_id: str
    execution_id: str
    execution_generation: int
    lease: LeaseGrant
    invocation_id: str
    entry_type: WorkerExecutionEntryType
    role: WorkerLLMRole | None
    content: WorkerExecutionMessageContent
    tool_call_id: str | None = None
    tool_name: str | None = None
    operation_key: str | None = None
    result_ref: str | None = None

    def __post_init__(self) -> None:
        """验证执行身份与记录种类的角色约束。"""
        if (
            self.lease.key.resource_type != "worker_execution"
            or self.lease.key.resource_id != self.execution_id
        ):
            raise ValueError("worker_message_lease_identity_mismatch")
        identity_values = (
            self.message_id,
            self.session_id,
            self.run_id,
            self.plan_id,
            self.task_id,
            self.execution_id,
            self.invocation_id,
        )
        if any(not value.strip() for value in identity_values):
            raise ValueError("worker_execution_message_identity_required")
        if self.execution_generation < 1:
            raise ValueError("execution_generation_must_be_positive")
        if self.entry_type == WorkerExecutionEntryType.LLM_MESSAGE:
            if self.role is None or self.content.tool_result is not None:
                raise ValueError("worker_llm_message_role_invalid")
        elif self.role is not None or self.content.tool_result is None:
            raise ValueError("worker_tool_result_content_invalid")


@dataclass(frozen=True, slots=True)
class WorkerExecutionMessage:
    """仓储读取的、带执行内顺序和记录时间的 Worker 消息。"""

    message_id: str
    session_id: str
    run_id: str
    plan_id: str
    task_id: str
    execution_id: str
    execution_generation: int
    invocation_id: str
    sequence_no: int
    entry_type: WorkerExecutionEntryType
    role: WorkerLLMRole | None
    content: WorkerExecutionMessageContent
    created_at: datetime
    tool_call_id: str | None = None
    tool_name: str | None = None
    operation_key: str | None = None
    result_ref: str | None = None


__all__ = [
    "WorkerExecutionEntryType",
    "WorkerExecutionMessage",
    "WorkerExecutionMessageAppend",
    "WorkerExecutionMessageContent",
    "WorkerLLMRole",
    "WorkerToolCallContent",
    "WorkerToolResultContent",
]
