"""图检索端口。"""

from __future__ import annotations

from typing import Protocol


class GraphSearchPort(Protocol):
    """按查询文本搜索图索引节点。"""

    async def search_nodes(self, query: str, *, limit: int = 12) -> list[dict[str, object]]: ...


GraphContextQueryPort = GraphSearchPort

__all__ = ["GraphContextQueryPort", "GraphSearchPort"]
