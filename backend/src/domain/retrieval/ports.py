"""跨存储检索能力的领域端口。"""

from __future__ import annotations

from typing import Protocol


class GraphContextQueryPort(Protocol):
    """查询图索引中的上下文摘要。"""

    async def query_context(
        self, query: str, *, limit: int = 12
    ) -> list[dict[str, object]]: ...


__all__ = ["GraphContextQueryPort"]
