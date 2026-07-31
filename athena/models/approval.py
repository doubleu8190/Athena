"""审批相关模型."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ApprovalRequest:
    """审批请求的完整数据结构.

    使用普通类（非 Pydantic）以容纳 asyncio.Future，
    Future 无法被 Pydantic 序列化。
    """

    __slots__ = (
        "id",
        "tool_name",
        "arguments",
        "risk_level",
        "timeout",
        "created_at",
        "session_id",
        "run_id",
        "future",
        "resolved",
        "resolution",
        "tool_call_id",
        "decided_at",
    )

    def __init__(
        self,
        id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        timeout: int,
        created_at: datetime,
        session_id: str,
        run_id: str,
        future: asyncio.Future[bool],
        tool_call_id: str | None = None,
    ) -> None:
        self.id = id
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_level = risk_level
        self.timeout = timeout
        self.created_at = created_at
        self.session_id = session_id
        self.run_id = run_id
        self.tool_call_id = tool_call_id
        self.future = future
        self.resolved: bool = False
        self.resolution: str = "pending"  # approved/denied/timeout
        self.decided_at: datetime | None = None


class ApprovalLog(BaseModel):
    """审批决策记录（持久化）."""

    id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    decision: ApprovalDecision
    decision_time_ms: float = 0  # 用户响应耗时
    timestamp: datetime
