"""工具相关模型."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    """风险等级."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolExecutionMode(StrEnum):
    """工具执行模式."""

    NATIVE = "native"  # 本地 Python 函数调用
    MCP = "mcp"  # 通过 MCP 协议远程代理


class ToolSchema(BaseModel):
    """工具 Schema 定义（MCP 风格）."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    require_approval: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    execution_mode: ToolExecutionMode = ToolExecutionMode.NATIVE


class ToolResult(BaseModel):
    """工具执行结果."""

    status: str  # success/failed/denied/timeout
    output: str | None = None
    error: str | None = None
    duration_ms: float = 0


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ToolCallRecord(BaseModel):
    """单次工具调用的完整记录."""

    id: str
    session_id: str
    step_id: str  # 所属步骤 ID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_output: str | None = None
    status: ToolCallStatus = ToolCallStatus.PENDING
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0
    error_message: str | None = None
    error_stack: str | None = None


class ToolConfig(BaseModel):
    """工具治理配置（持久化）.

    用户对工具的治理参数调优记录，跨重启持久化。
    """

    tool_name: str
    execution_mode: ToolExecutionMode
    server_name: str | None = None
    remote_name: str | None = None
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    require_approval: bool = True
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
