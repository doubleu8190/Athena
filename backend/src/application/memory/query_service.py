"""记忆只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.memory.query_ports import MemoryQueryPort


class MemoryQueryService:
    def __init__(self, port: MemoryQueryPort) -> None:
        self._port = port

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._port.search(**kwargs)

    async def list_memories(self, **kwargs: Any) -> dict[str, Any]:
        return await self._port.list_memories(**kwargs)

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        return await self._port.get(memory_id)

    async def revisions(self, memory_id: str) -> list[dict[str, Any]]:
        return await self._port.revisions(memory_id)


__all__ = ["MemoryQueryService"]
