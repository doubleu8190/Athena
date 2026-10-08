"""检索运行历史的只读查询用例。"""

from __future__ import annotations

from typing import Any

from domain.retrieval.query_ports import RetrievalQueryPort


class RetrievalQueryService:
    def __init__(self, port: RetrievalQueryPort) -> None:
        self._port = port

    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        return await self._port.list_runs(**filters)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._port.get_run(run_id)


__all__ = ["RetrievalQueryService"]
