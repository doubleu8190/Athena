"""运行列表查询用例。"""

from __future__ import annotations

from domain.runs import RunQueryPort, RunStatus, RunSummary
from domain.sessions import SessionRepository

from application.common.responses import RunSummaryResponse

from application.sessions import SessionNotFoundError
from .command_service import RunNotFoundError


class RunQueryService:
    """把会话存在性校验和运行查询组合起来。"""

    def __init__(self, sessions: SessionRepository, runs: RunQueryPort) -> None:
        self._sessions = sessions
        self._runs = runs

    async def list_for_session(self, session_id: str) -> list[RunSummaryResponse]:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [_to_response(value) for value in await self._runs.list_for_session(session_id)]

    async def get(self, run_id: str) -> RunSummaryResponse:
        value = await self._runs.get(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return _to_response(value)


def _to_response(value: RunSummary) -> RunSummaryResponse:
    """将运行摘要实体转换为应用层响应。"""
    return RunSummaryResponse(
        run_id=value.run_id,
        session_id=value.session_id,
        status=value.status.value if isinstance(value.status, RunStatus) else str(value.status),
        root_thread_id=value.root_thread_id,
        error=value.error,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )

__all__ = ["RunNotFoundError", "RunQueryService"]
