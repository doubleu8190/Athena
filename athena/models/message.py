"""消息模型."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(BaseModel):
    """对话消息."""

    id: str
    session_id: str
    role: MessageRole
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str | None = None
    run_id: str | None = None  # 所属运行 ID（一次用户请求 ≈ 一个 run），前端按此分组
    # 平铺自 metadata_json 的字段
    step_id: str | None = (
        None  # 关联的步骤 ID（assistant 指向 llm_call 步骤，tool 指向工具步骤）
    )
    tool_call_record_id: str | None = None  # 关联的 tool_call 记录 ID（tool 消息）
    tool_name: str | None = None  # 工具名（tool 消息，前端据此标名避免跨表 join）
    type: str | None = None  # 消息类型标记（如 "conversation_summary"）
    timestamp: datetime
