"""长期记忆后台任务端口。"""

from __future__ import annotations

from typing import Protocol


class MemoryJobPort(Protocol):
    async def recover(self) -> None: ...
    async def claim_next_job(self) -> dict[str, object] | None: ...
    async def mark_succeeded(self, turn_id: str) -> None: ...
    async def mark_failed(self, turn_id: str, error: str, *, retry: bool) -> None: ...

__all__ = ["MemoryJobPort"]
