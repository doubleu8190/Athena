"""长期记忆后台任务端口。"""

from __future__ import annotations

from typing import Protocol

from domain.leases import LeaseGrant
from .entities import ClaimedMemoryJob, MemoryJobStatus


class MemoryJobStatusPort(Protocol):
    """Read-only management view of durable memory job progress."""

    async def list_recent(
        self,
        *,
        limit: int,
        offset: int,
        session_id: str | None = None,
    ) -> tuple[list[MemoryJobStatus], int]: ...


class MemoryJobPort(Protocol):
    """记忆任务入队能力，供运行终态使用。"""

    async def enqueue(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str = "",
    ) -> bool: ...


class MemoryJobQueue(Protocol):
    """记忆 worker 使用的任务领取和完成能力。"""

    async def claim_next_job(
        self, *, lease_seconds: float = 60.0
    ) -> ClaimedMemoryJob | None: ...
    async def mark_succeeded(self, turn_id: str, *, lease: LeaseGrant) -> None: ...
    async def mark_failed(
        self, turn_id: str, error: str, *, retry: bool, lease: LeaseGrant
    ) -> None: ...


__all__ = ["MemoryJobPort", "MemoryJobQueue", "MemoryJobStatusPort"]
