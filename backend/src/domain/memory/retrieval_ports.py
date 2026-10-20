"""长期记忆检索端口。"""

from __future__ import annotations

from typing import Protocol


class MemoryVectorSearchPort(Protocol):
    async def query(self, text: str, limit: int = 10, *, session_id: str | None = None) -> list[dict[str, object]]: ...


class MemoryVectorIndexPort(Protocol):
    async def index(self, memory_id: str, content: str) -> None: ...


__all__ = ["MemoryVectorIndexPort", "MemoryVectorSearchPort"]
