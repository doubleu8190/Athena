"""审批外部能力端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import ApprovalDecision, ApprovalRequest


class ApprovalRepository(Protocol):
    async def get(self, approval_id: str) -> ApprovalRequest | None: ...
    async def list_pending(self, session_id: str | None = None) -> list[ApprovalRequest]: ...
    async def list_history(self, session_id: str | None = None, *, limit: int = 50, offset: int = 0) -> list[ApprovalRequest]: ...
    async def resolve(self, approval_id: str, *, decision: ApprovalDecision) -> bool: ...
    async def expire_due_approvals(self, *, limit: int = 64) -> int: ...


__all__ = ["ApprovalRepository"]
