"""长期记忆只读查询端口。"""

from __future__ import annotations

from typing import Any, Protocol


class MemoryQueryPort(Protocol):
    async def search(self, *, query: str, n_results: int, where: dict[str, Any] | None) -> list[dict[str, Any]]: ...
    async def list_memories(self, *, limit: int, offset: int, expired: bool, session_id: str | None) -> dict[str, Any]: ...
    async def get(self, memory_id: str) -> dict[str, Any] | None: ...
    async def revisions(self, memory_id: str) -> list[dict[str, Any]]: ...


__all__ = ["MemoryQueryPort"]
