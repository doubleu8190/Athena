"""检索运行查询端口。"""

from __future__ import annotations

from typing import Any, Protocol


class RetrievalQueryPort(Protocol):
    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]: ...
    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...


__all__ = ["RetrievalQueryPort"]
