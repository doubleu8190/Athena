"""检索运行查询端口。"""

from __future__ import annotations

from typing import Protocol

from .trace_contracts import RetrievalCandidateSelection, RetrievalCandidateTrace, RetrievalProviderTrace


class RetrievalPort(Protocol):
    async def list_runs(
        self,
        *,
        limit: int = 30,
        offset: int = 0,
        scope: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        query: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[dict[str, object]], int]: ...

    async def get_run(self, run_id: str) -> dict[str, object] | None: ...

    async def record_context(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_run_id: str | None,
        message_id: str | None,
        query: str,
        providers: tuple[RetrievalProviderTrace, ...],
        candidates: tuple[RetrievalCandidateTrace, ...],
        duration_ms: float,
        status: str,
        error_message: str | None,
    ) -> str: ...

    async def update_context_selection(
        self,
        *,
        run_id: str,
        selections: tuple[RetrievalCandidateSelection, ...],
    ) -> bool: ...

__all__ = ["RetrievalPort"]
