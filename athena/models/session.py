"""会话模型."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SessionStatus(StrEnum):
    """会话状态.

    状态流转：idle → running → idle (正常完成)
                    │
                    ▼
              interrupted → recovering → idle (恢复成功)
                                    │
                                    ▼
                                  failed (恢复失败)
    """

    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    FAILED = "failed"


class Session(BaseModel):
    """会话记录."""

    id: str
    title: str = "New Session"
    status: SessionStatus = SessionStatus.IDLE
    run_id: str | None = None  # 当前运行 ID，关联 steps 表
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
