"""Run 实时事件传输的显式类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.run.model.entities import (
    PendingSubagentApproval,
    PendingSubagentInput,
    Run,
    RunEvent,
    RunStatus,
    SubagentTaskSummary,
)

MAX_RUN_SSE_EVENT_BYTES = 64 * 1024
MAX_RUN_SSE_SNAPSHOT_BYTES = 512 * 1024


class RunStreamMessage(BaseModel):
    """快照中可展示的正式对话消息投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    role: Literal["user", "assistant"]
    status: str
    text: str
    sequence: int
    created_at: datetime
    # 向后兼容的增量自包含字段：与 GET /api/v1/sessions/{id}/messages 响应结构对齐，
    # 供客户端在终态快照到达时直接合并完整消息与附件 parts，无需额外翻页查库。
    session_id: str | None = None
    run_id: str | None = None
    reply_to_message_id: str | None = None
    parts: tuple[dict[str, object], ...] | None = None
    completed_at: datetime | None = None


class RunSnapshotPayload(BaseModel):
    """订阅成功后的数据库权威快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RunStatus
    event_sequence: int
    report_message_id: str | None = None
    assistant_message_id: str | None = None
    messages: tuple[RunStreamMessage, ...]
    events: tuple[RunEvent, ...]
    messages_complete: bool = True
    messages_before_sequence: int | None = None
    events_complete: bool = True
    events_after_sequence: int | None = None
    subagent_tasks: tuple[SubagentTaskSummary, ...] = ()
    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = ()
    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = ()


class RunTextDeltaPayload(BaseModel):
    """不持久化、只在当前执行代次内有效的临时文本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    round: int = Field(default=0, ge=0)


class RunPersistedEventPayload(BaseModel):
    """数据库已提交的 Run 状态事件通知。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: RunEvent


class RunInterruptedPayload(BaseModel):
    """Redis 订阅中断后的重新对账提示。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Literal["redis_reconnected", "redis_unavailable", "stream_gap", "publisher_error"]


class RunHeartbeatPayload(BaseModel):
    """保持 SSE 连接存活的心跳。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime


class RunSnapshotEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["snapshot"] = "snapshot"
    run_id: str
    execution_epoch: int
    stream_sequence: int
    payload: RunSnapshotPayload


class RunTextDeltaEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text_delta"] = "text_delta"
    run_id: str
    execution_epoch: int
    stream_sequence: int
    payload: RunTextDeltaPayload


class RunPersistedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["run_event"] = "run_event"
    run_id: str
    execution_epoch: int
    stream_sequence: int
    payload: RunPersistedEventPayload


class RunInterruptedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["stream_interrupted"] = "stream_interrupted"
    run_id: str
    execution_epoch: int
    stream_sequence: int
    payload: RunInterruptedPayload


class RunHeartbeatEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["heartbeat"] = "heartbeat"
    run_id: str
    execution_epoch: int
    stream_sequence: int
    payload: RunHeartbeatPayload


type RunStreamEvent = Annotated[
    RunSnapshotEvent
    | RunTextDeltaEvent
    | RunPersistedEvent
    | RunInterruptedEvent
    | RunHeartbeatEvent,
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class RunStreamSnapshot:
    """同一 PostgreSQL 快照读取的 Run、正式消息和状态事件。"""

    run: Run
    messages: tuple[RunStreamMessage, ...]
    events: tuple[RunEvent, ...]
    messages_complete: bool = True
    messages_before_sequence: int | None = None
    events_complete: bool = True
    events_after_sequence: int | None = None
    subagent_tasks: tuple[SubagentTaskSummary, ...] = ()
    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = ()
    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = ()
    assistant_message_id: str | None = None


__all__ = [
    "MAX_RUN_SSE_EVENT_BYTES",
    "MAX_RUN_SSE_SNAPSHOT_BYTES",
    "RunHeartbeatEvent",
    "RunHeartbeatPayload",
    "RunInterruptedEvent",
    "RunInterruptedPayload",
    "RunPersistedEvent",
    "RunPersistedEventPayload",
    "RunSnapshotEvent",
    "RunSnapshotPayload",
    "RunStreamEvent",
    "RunStreamMessage",
    "RunStreamSnapshot",
    "RunTextDeltaEvent",
    "RunTextDeltaPayload",
]
