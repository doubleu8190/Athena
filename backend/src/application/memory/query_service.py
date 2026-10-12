"""记忆只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.memory.query_ports import MemoryQueryPort
from application.common.responses import MemoryPageResponse, MemoryResponse
from domain.memory import MemoryRecord, MemoryPage


class MemoryQueryService:
    def __init__(self, port: MemoryQueryPort) -> None:
        self._port = port

    async def search(self, **kwargs: Any) -> list[MemoryResponse]:
        return [_to_response(value) for value in await self._port.search(**kwargs)]

    async def list_memories(self, **kwargs: Any) -> MemoryPageResponse:
        value = await self._port.list_memories(**kwargs)
        if isinstance(value, MemoryPage):
            return MemoryPageResponse(tuple(_to_response(item) for item in value.items), value.total, dict(value.stats))
        if isinstance(value, dict):
            items = tuple(_to_response(item) for item in value.get("items", ()))
            return MemoryPageResponse(items, int(value.get("total", len(items))), {key: int(item) for key, item in value.items() if key != "items" and isinstance(item, int)})
        return MemoryPageResponse((), 0, {})

    async def get(self, memory_id: str) -> MemoryResponse | None:
        value = await self._port.get(memory_id)
        return None if value is None else _to_response(value)

    async def revisions(self, memory_id: str) -> list[MemoryResponse]:
        return [_to_response(value) for value in await self._port.revisions(memory_id)]


def _to_response(value: Any) -> MemoryResponse:
    """将记忆端口的记录转换为应用响应。"""
    if isinstance(value, MemoryResponse):
        return value
    if isinstance(value, MemoryRecord):
        return MemoryResponse(value.id, value.content, dict(value.metadata), value.score, value.source, value.status.value)
    return MemoryResponse(str(value.get("id", "")), str(value.get("content", "")), dict(value.get("metadata") or {}), value.get("score"), value.get("source"), str(value.get("status", "active")))


__all__ = ["MemoryQueryService"]
