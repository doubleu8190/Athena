"""无框架依赖的应用事件信封。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class EventDurability(StrEnum):
    REALTIME = "realtime"
    SNAPSHOT = "snapshot"
    DURABLE = "durable"


class EventType(StrEnum):
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    PLAN_CREATED = "plan.created"
    PLAN_COMPLETED = "plan.completed"
    PLAN_FAILED = "plan.failed"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    STREAM_SNAPSHOT = "stream.snapshot"
    RUN_STARTED = "run.started"
    RUN_ROOT_SUSPENDED = "run.root_suspended"
    RUN_RESUMED = "run.resumed"
    RUN_CANCELLED = "run.cancelled"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    ATTACHMENT_UPDATED = "attachment_updated"
    FILE_PROCESSING_STARTED = "file_processing_started"
    FILE_PROCESSING_COMPLETED = "file_processing_completed"
    FILE_PROCESSING_FAILED = "file_processing_failed"
    AGENT_WAITING_FILE = "agent.waiting_for_files"
    TASK_QUEUED = "task.queued"
    TASK_RETRIED = "task.retrying"
    TASK_CANCELLED = "task.cancelled"
    SYNTHESIS_STARTED = "synthesis.started"
    SYNTHESIS_COMPLETED = "synthesis.completed"
    BUDGET_EXCEEDED = "run.budget_exceeded"
    LLM_COMPLETED = "llm.completed"
    LLM_STARTED = "llm.started"
    LLM_VALIDATION_FAILED = "llm.validation_failed"
    THINKING_STARTED = "thinking.started"
    THINKING_SUMMARY = "thinking.summary"
    THINKING_COMPLETED = "thinking.completed"
    TOOL_COMPLETED = "tool.completed"
    TOOL_STARTED = "tool.started"
    MESSAGE_PERSISTED = "message.persisted"


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    """可持久化或实时广播的应用事件。"""

    event_type: EventType | str
    durability: EventDurability | str
    session_id: str
    session_seq: int | None = None
    run_id: str | None = None
    message_id: str | None = None
    attachment_id: str | None = None
    stream_id: str | None = None
    stream_type: str | None = None
    chunk_id: int | None = None
    is_complete: bool = False
    parent_run_id: str | None = None
    transition_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = field(default_factory=dict)
