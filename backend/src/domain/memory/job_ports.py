"""长期记忆后台任务端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import ClaimedMemoryJob


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

    async def recover(self) -> None: ...
    async def claim_next_job(self) -> ClaimedMemoryJob | None: ...
    async def mark_succeeded(self, turn_id: str) -> None: ...
    async def mark_failed(self, turn_id: str, error: str, *, retry: bool) -> None: ...


__all__ = ["MemoryJobPort", "MemoryJobQueue"]
