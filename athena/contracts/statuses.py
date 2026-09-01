"""Agent Store 与 Runtime 共享的有限状态值。"""

from __future__ import annotations

from enum import StrEnum


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_FILES = "waiting_files"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentCommandStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class StreamSnapshotStatus(StrEnum):
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentApprovalStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class AgentApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ToolExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolSideEffectClass(StrEnum):
    UNKNOWN = "unknown"
