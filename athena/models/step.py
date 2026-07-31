"""执行步骤模型.

步骤定义规范：
- 一次 LLM 调用 = 一个 llm_call 类型 step
- 一次工具执行 = 一个 tool_execution 类型 step
- step_number 在同一 run_id 范围内全局递增
- 工具执行步骤的 parent_step_id 指向产生它的 LLM 调用步骤
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StepType(StrEnum):
    LLM_CALL = "llm_call"  # LLM 调用步骤
    TOOL_EXECUTION = "tool_execution"  # 单个工具执行步骤


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Step(BaseModel):
    """Agent 执行过程中的一个步骤."""

    id: str
    session_id: str
    run_id: str  # 本次 Agent 运行的 ID
    step_number: int  # 步骤全局序号（从 1 开始递增）
    step_type: StepType
    parent_step_id: str | None = None  # 父步骤 ID（工具执行步骤引用 LLM 调用步骤）
    status: StepStatus = StepStatus.PENDING
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0
    llm_input_tokens: int = 0  # 仅 llm_call 类型有效
    llm_output_tokens: int = 0  # 仅 llm_call 类型有效
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
