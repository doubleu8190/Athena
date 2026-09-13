"""供状态投影和事件传输使用的应用事件契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class EventDurability(StrEnum):
    REALTIME = "realtime"
    SNAPSHOT = "snapshot"
    DURABLE = "durable"


class EventType(StrEnum):
    """稳定的点分隔应用事件名称。"""

    APPROVAL_REQUIRED = "approval.required"
    AGENT_WAITING_FILE = "agent.waiting_for_files"
    SUB_AGENT_COMPLETE = "subagent.completed"
    SUB_AGENT_FAILED = "subagent.failed"
    SUB_AGENT_SPAWNED = "subagent.started"
    PLAN_CREATED = "plan.created"
    PLAN_COMPLETED = "plan.completed"
    PLAN_FAILED = "plan.failed"
    PLAN_CANCELLED = "plan.cancelled"
    TASK_QUEUED = "task.queued"
    TASK_STARTED = "task.started"
    TASK_RETRIED = "task.retrying"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    SYNTHESIS_STARTED = "synthesis.started"
    SYNTHESIS_COMPLETED = "synthesis.completed"
    BUDGET_EXCEEDED = "run.budget_exceeded"
    LLM_CALL_END = "llm.completed"
    LLM_CALL_START = "llm.started"
    LLM_TOKEN = "message.delta"
    STREAM_END = "message.completed"
    STREAM_START = "message.started"
    STREAM_SNAPSHOT = "stream.snapshot"
    THINKING_STARTED = "thinking.started"
    THINKING_SUMMARY = "thinking.summary"
    THINKING_COMPLETED = "thinking.completed"
    TOOL_CALL_END = "tool.completed"
    TOOL_CALL_START = "tool.started"
    MESSAGE_PERSISTED = "message.persisted"
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_CANCELLED = "run.cancelled"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ATTACHMENT_UPDATED = "attachment_updated"
    FILE_PROCESSING_STARTED = "file_processing_started"
    FILE_PROCESSING_COMPLETED = "file_processing_completed"
    FILE_PROCESSING_FAILED = "file_processing_failed"


class ApplicationEvent(BaseModel):
    """在持久化、Runtime 和客户端投影之间传递的应用事件。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=2, ge=1)
    session_seq: int | None = Field(default=None, ge=1)
    """会话级 SSE 游标；由事件存储分配。"""
    event_type: EventType
    durability: EventDurability
    session_id: str = Field(min_length=1)
    run_id: str | None = None
    message_id: str | None = None
    attachment_id: str | None = None
    stream_id: str | None = None
    stream_type: str | None = None
    chunk_id: int | None = Field(default=None, ge=1)
    is_complete: bool = False
    parent_run_id: str | None = None
    transition_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
