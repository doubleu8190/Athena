"""会话 Controller 的新实现。

该 Router 不自行查数据库。应用启动时通过 ``service_factory`` 注入
``SessionService``，因此 HTTP 层可以独立测试。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from application.runs import RunQueryService
from application.sessions import (
    SessionMessageQueryService,
    SessionNotFoundError,
    SessionService,
)
from interfaces.http.dto.sessions import (
    CreateSessionRequest,
    SessionDeletionResponse,
    SessionResponse,
    UpdateSessionRequest,
    MessageResponse,
    RunSummaryResponse,
)


def build_sessions_router(
    service_factory: Callable[[], SessionService],
    *,
    message_query_factory: Callable[[], SessionMessageQueryService] | None = None,
    run_query_factory: Callable[[], RunQueryService] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["sessions"])

    def service() -> SessionService:
        return service_factory()

    def message_query() -> SessionMessageQueryService:
        if message_query_factory is None:
            raise RuntimeError("message query service is not configured")
        return message_query_factory()

    def run_query() -> RunQueryService:
        if run_query_factory is None:
            raise RuntimeError("run query service is not configured")
        return run_query_factory()

    @router.post("", response_model=SessionResponse)
    async def create_session(
        request: CreateSessionRequest,
        sessions: SessionService = Depends(service),
    ) -> SessionResponse:
        return SessionResponse.from_domain(await sessions.create(request.title))

    @router.get("", response_model=list[SessionResponse])
    async def list_sessions(
        sessions: SessionService = Depends(service),
    ) -> list[SessionResponse]:
        return [
            SessionResponse.from_domain(item) for item in await sessions.list()
        ]

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: str,
        sessions: SessionService = Depends(service),
    ) -> SessionResponse:
        try:
            return SessionResponse.from_domain(await sessions.get(session_id))
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc

    @router.patch("/{session_id}", response_model=SessionResponse)
    async def rename_session(
        session_id: str,
        request: UpdateSessionRequest,
        sessions: SessionService = Depends(service),
    ) -> SessionResponse:
        try:
            updated = await sessions.rename(session_id, request.title)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="title_empty") from exc
        return SessionResponse.from_domain(updated)

    @router.delete("/{session_id}", response_model=SessionDeletionResponse)
    async def delete_session(
        session_id: str,
        sessions: SessionService = Depends(service),
    ) -> SessionDeletionResponse:
        try:
            await sessions.delete(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return SessionDeletionResponse(status="deleted", session_id=session_id)

    @router.get("/{session_id}/messages", response_model=list[MessageResponse])
    async def list_session_messages(
        session_id: str,
        limit: int | None = None,
        messages: SessionMessageQueryService = Depends(message_query),
    ) -> list[MessageResponse]:
        try:
            values = await messages.list_messages(session_id, limit=limit)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return [MessageResponse.from_domain(value) for value in values]

    @router.get("/{session_id}/runs", response_model=list[RunSummaryResponse])
    async def list_session_runs(
        session_id: str,
        runs: RunQueryService = Depends(run_query),
    ) -> list[RunSummaryResponse]:
        try:
            values = await runs.list_for_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return [RunSummaryResponse.from_domain(value) for value in values]

    return router
