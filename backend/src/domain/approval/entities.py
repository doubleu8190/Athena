"""审批领域实体。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk_level: str
    task_id: str | None = None
    plan_id: str | None = None
    approval_batch_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None


__all__ = ["ApprovalDecision", "ApprovalRequest", "ApprovalStatus"]
