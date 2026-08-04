"""WebSocket 事件 Schema — 事件类型定义与消息构建工具.

事件类型按功能分类，对应文档 6.1.2 节。
所有事件统一格式：{type, session_id, run_id, timestamp, data}
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """服务器 → 客户端事件类型."""

    # 会话生命周期
    SESSION_START = "session_start"
    SESSION_COMPLETE = "session_complete"
    SESSION_INTERRUPTED = "session_interrupted"
    SESSION_RECOVERED = "session_recovered"
    STREAM_START = "stream_start"
    STREAM_END = "stream_end"

    # LLM 调用
    LLM_CALL_START = "llm_call_start"
    LLM_TOKEN = "llm_token"
    LLM_CALL_END = "llm_call_end"

    # 工具执行
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_SKIPPED = "tool_call_skipped"
    TOOL_INTERRUPTED = "tool_interrupted"

    # 审批生命周期
    APPROVAL_QUEUE_STATUS = "approval_queue_status"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_PROCESSING = "approval_processing"
    APPROVAL_RESULT = "approval_result"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_INTERRUPTED = "approval_interrupted"

    # 子 Agent 生命周期
    SUB_AGENT_SPAWNED = "sub_agent_spawned"
    SUB_AGENT_START = "sub_agent_start"
    SUB_AGENT_PROGRESS = "sub_agent_progress"
    SUB_AGENT_COMPLETE = "sub_agent_complete"
    SUB_AGENT_FAILED = "sub_agent_failed"

    # 记忆系统
    MEMORY_EXTRACTED = "memory_extracted"
    MEMORY_SUMMARY_START = "memory_summary_start"
    MEMORY_SUMMARY_COMPLETE = "memory_summary_complete"
    MEMORY_SEARCH_START = "memory_search_start"
    MEMORY_SEARCH_COMPLETE = "memory_search_complete"
    MEMORY_SAVED = "memory_saved"

    # 上下文压缩
    CONTEXT_COMPRESS_START = "context_compress_start"
    CONTEXT_COMPRESS_COMPLETE = "context_compress_complete"
    CONTEXT_SUMMARY_SAVED = "context_summary_saved"

    # 恢复与错误
    RECOVERY_START = "recovery_start"
    RECOVERY_STEP = "recovery_step"
    RECOVERY_COMPLETE = "recovery_complete"
    ERROR = "error"
    ERROR_RECOVERED = "error_recovered"

    # 系统
    BUDGET_EXCEEDED = "budget_exceeded"
    SAFETY_WARNING = "safety_warning"
    PONG = "pong"
    SYSTEM_NOTICE = "system_notice"
    SYSTEM_MESSAGE = "system_message"


class ClientEventType(StrEnum):
    """客户端 → 服务器事件类型."""

    USER_COMMAND = "user_command"
    APPROVAL_RESPONSE = "approval_response"
    APPROVAL_CANCEL = "approval_cancel"
    SESSION_STOP = "session_stop"
    SESSION_RESUME = "session_resume"
    MEMORY_SAVE = "memory_save"
    PING = "ping"


class Event(BaseModel):
    """通用事件消息格式."""

    type: str
    session_id: str | None = None
    run_id: str | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    data: dict[str, Any] = Field(default_factory=dict)


def build_event(
    event_type: str | EventType,
    data: dict[str, Any],
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """构建标准事件消息字典."""
    return {
        "type": str(event_type),
        "session_id": session_id,
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "data": data,
    }
