"""验证图检索在过滤旧版本后继续读取后续页。"""

import pytest

from application.retrieval.graph_service import GraphContextProvider
from application.runs.context.contracts import ContextPlan


@pytest.mark.asyncio
async def test_graph_retrieval_pages_past_old_revisions() -> None:
    """旧节点超过一页时，仍能找到后面的有效节点。"""
    candidates = [
        {"id": f"node-{index:03}", "content": "match", "active": index == 65}
        for index in range(66)
    ]

    class Graph:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def search_nodes(self, query: str, *, limit: int, after_id: str | None):
            self.cursors.append(after_id)
            return [row for row in candidates if after_id is None or row["id"] > after_id][:limit]

    async def active(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [row for row in rows if row["active"]]

    graph = Graph()
    provider = GraphContextProvider(graph, limit=1, active_revision_filter=active)
    result = await provider.retrieve_context(
        session_id="session", run_id="run", plan=ContextPlan(graph_query="match", max_items=1)
    )
    assert result.status == "succeeded"
    assert [item.source_id for item in result.items] == ["node-065"]
    assert graph.cursors == [None, "node-031", "node-063"]
