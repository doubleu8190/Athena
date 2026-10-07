"""运行列表查询用例。"""

from __future__ import annotations

from domain.runs import RunQueryPort, RunStatus, RunSummary
from domain.sessions import SessionRepository

from application.sessions import SessionNotFoundError
from .command_service import RunNotFoundError


class RunQueryService:
    """把会话存在性校验和运行查询组合起来。"""

    def __init__(self, sessions: SessionRepository, runs: RunQueryPort) -> None:
        self._sessions = sessions
        self._runs = runs

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return await self._runs.list_for_session(session_id)

    async def get(self, run_id: str) -> RunSummary:
        value = await self._runs.get(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return value

__all__ = ["RunNotFoundError", "RunQueryService"]
