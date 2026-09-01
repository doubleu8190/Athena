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

    AGENT_WAITING_FILE = "agent.waiting_for_files"
    PARALLEL_AGENTS_STARTED = "subagent.started"
    SUB_AGENT_COMPLETE = "subagent.completed"
    SUB_AGENT_FAILED = "subagent.failed"
    SUB_AGENT_SPAWNED = "subagent.started"
    SYSTEM_MESSAGE = "message.system"
    BUDGET_EXCEEDED = "run.budget_exceeded"
    ERROR = "run.failed"
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


class ApplicationEvent(BaseModel):
    """在持久化、Runtime 和客户端投影之间传递的应用事件。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=2, ge=1)
    session_seq: int | None = Field(default=None, ge=1)
    """会话级 SSE 游标；由事件存储分配。"""
    event_id: int | None = Field(default=None, ge=1)
    event_type: str = Field(min_length=1)
    durability: EventDurability
    session_id: str = Field(min_length=1)
    run_id: str | None = None
    stream_id: str | None = None
    stream_type: str | None = None
    chunk_id: int | None = Field(default=None, ge=1)
    is_complete: bool = False
    parent_run_id: str | None = None
    producer_id: str = Field(default="runtime-main", min_length=1)
    sequence: int = Field(default=0, ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
