"""Gateway 命令与会话接口的请求、响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from athena.contracts.commands import CommandType
from athena.contracts.statuses import AgentCommandStatus, AgentRunStatus


class SessionDeletionResponse(BaseModel):
    """删除会话后的响应。"""

    status: str
    session_id: str


class RunSummaryResponse(BaseModel):
    """会话运行记录的公开摘要。"""

    run_id: str
    session_id: str
    status: AgentRunStatus
    created_by_command_id: str | None = None
    pause_requested: bool
    cancel_requested: bool
    error: str | None = None
    created_at: str
    updated_at: str


class SubmitRunResponse(BaseModel):
    """提交消息命令后的响应。"""

    command_id: str
    run_id: str | None = None
    message_id: str
    attachment_ids: list[str] = Field(default_factory=list)
    status: AgentCommandStatus
    deduplicated: bool


class ControlCommandResponse(BaseModel):
    """暂停或恢复命令提交后的响应。"""

    command_id: str
    status: AgentCommandStatus


class CancelCommandResponse(BaseModel):
    """取消命令提交后的响应。"""

    command_id: str
    run_id: str | None = None
    status: AgentCommandStatus


class CommandStatusResponse(BaseModel):
    """命令处理状态、结果和错误信息。"""

    command_id: str
    session_id: str
    run_id: str | None = None
    command_type: CommandType
    status: AgentCommandStatus
    attempt: int
    result: Any = None
    error: Any = None
