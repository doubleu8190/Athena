"""工具治理和 MCP 配置领域实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RiskLevel(StrEnum):
    """工具风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolExecutionMode(StrEnum):
    """工具执行来源。"""

    NATIVE = "native"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class JsonSchema:
    """工具参数 JSON Schema。"""

    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """工具治理配置。"""

    tool_name: str
    execution_mode: ToolExecutionMode
    server_name: str | None
    remote_name: str | None
    description: str
    parameters: JsonSchema
    risk_level: RiskLevel
    require_approval: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """MCP 服务端启动配置。"""

    command: str | tuple[str, ...]
    args: tuple[str, ...]
    env: dict[str, str]
    image_id: str | None
    network_policy: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class MCPServer:
    """已注册的 MCP 服务端。"""

    name: str
    config: MCPServerConfig
    created_at: datetime
    deleted_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    """一次工具调用的可信上下文和参数。"""

    tool_name: str
    arguments: dict[str, Any]
    session_id: str
    run_id: str
    tool_call_id: str
    approval_decision: str | None = None
    agent_role: str = "root"
    plan_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """工具执行的稳定结果。"""

    status: str
    output: str | None = None
    error: str | None = None
    duration_ms: float = 0
