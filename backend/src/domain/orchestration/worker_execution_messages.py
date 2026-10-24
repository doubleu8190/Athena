"""Worker 模型交互和工具回执的持久消息契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class WorkerExecutionEntryType(StrEnum):
    """Worker 执行轨迹中的记录种类。"""

    LLM_MESSAGE = "llm_message"
    TOOL_RESULT = "tool_result"


class WorkerLLMRole(StrEnum):
    """Worker 实际发送或接收的模型消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class WorkerToolCallContent(BaseModel):
    """Worker assistant 回复中的一个规范化工具调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]


class WorkerToolResultContent(BaseModel):
    """Worker 工具回执中模型可见的结果部分。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: int | None = None
    result_ref: str | None = None
    truncated: bool | None = None


class WorkerExecutionMessageContent(BaseModel):
    """Worker 消息正文及其结构化工具信息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: Any = None
    tool_calls: tuple[WorkerToolCallContent, ...] = ()
    tool_result: WorkerToolResultContent | None = None


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
