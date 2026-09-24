"""Agent Store 与 Runtime 共享的有限状态值。"""

from __future__ import annotations

from enum import StrEnum


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentCommandStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class AgentApprovalStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class AgentApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
