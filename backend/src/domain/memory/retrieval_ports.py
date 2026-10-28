"""长期记忆检索端口。"""

from __future__ import annotations

from typing import Protocol


class MemoryVectorSearchPort(Protocol):
    async def query(self, text: str, limit: int = 10, *, session_id: str | None = None) -> list[dict[str, object]]: ...


__all__ = ["MemoryVectorSearchPort"]
