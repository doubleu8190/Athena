"""检索运行查询端口。"""

from __future__ import annotations

from typing import Any, Protocol


class RetrievalPort(Protocol):
    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]: ...
    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    async def record_context(self, *, session_id: str, agent_run_id: str | None, query: str, provider_results: list[Any],
                       items: list[Any]) -> str: ...

__all__ = ["RetrievalPort"]
