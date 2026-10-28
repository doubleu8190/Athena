"""会话 Controller 的新实现。

该 Router 不自行查数据库。应用启动时通过 ``service_factory`` 注入
``SessionService``，因此 HTTP 层可以独立测试。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from application.runs import RunQueryService
from application.sessions import (
    SessionMessageQueryService,
    SessionNotFoundError,
    SessionService,
)
from application.memory import SessionMemorySettingsService
from interfaces.http.dto.sessions import (
    CreateSessionRequest,
    SessionDeletionResponse,
    SessionResponse,
    UpdateSessionRequest,
    SessionMemorySettingsRequest,
    SessionMemorySettingsResponse,
    MessageResponse,
    RunSummaryResponse,
)
from interfaces.http.dto.runs import RunTimelineResponse


def build_sessions_router(
    service_factory: Callable[[], SessionService],
    *,
    message_query_factory: Callable[[], SessionMessageQueryService] | None = None,
    run_query_factory: Callable[[], RunQueryService] | None = None,
    memory_settings_factory: Callable[[], SessionMemorySettingsService] | None = None,
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

    def memory_settings() -> SessionMemorySettingsService:
        if memory_settings_factory is None:
            raise RuntimeError("session memory settings service is not configured")
        return memory_settings_factory()

    @router.post("", response_model=SessionResponse)
    async def create_session(
        request: CreateSessionRequest,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        sessions: SessionService = Depends(service),
    ) -> SessionResponse:
        try:
            value = await sessions.create(
                project_id=request.project_id,
                creation_request_id=idempotency_key,
                title=request.title,
            )
        except LookupError as error:
            detail = str(error)
            raise HTTPException(status_code=404, detail=detail) from error
        except ValueError as error:
            if str(error) in {"idempotency_conflict", "session_deleted"}:
                raise HTTPException(status_code=409, detail=str(error)) from error
            raise
        if value.idempotency_replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return SessionResponse.from_application(value)

    @router.get("", response_model=list[SessionResponse])
    async def list_sessions(
        project_id: str | None = Query(default=None),
        sessions: SessionService = Depends(service),
    ) -> list[SessionResponse]:
        return [
            SessionResponse.from_application(item)
            for item in await sessions.list_sessions(project_id=project_id)
        ]

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: str,
        sessions: SessionService = Depends(service),
    ) -> SessionResponse:
        try:
            return SessionResponse.from_application(await sessions.get(session_id))
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
            detail = "title_too_long" if str(exc) == "title_too_long" else "title_empty"
            raise HTTPException(status_code=400, detail=detail) from exc
        return SessionResponse.from_application(updated)

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

    if memory_settings_factory is not None:
        @router.get(
            "/{session_id}/memory-settings",
            response_model=SessionMemorySettingsResponse,
        )
        async def get_session_memory_settings(
            session_id: str,
            settings: SessionMemorySettingsService = Depends(memory_settings),
        ) -> SessionMemorySettingsResponse:
            try:
                value = await settings.get(session_id)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="session_not_found") from exc
            return SessionMemorySettingsResponse(
                session_id=value.session_id,
                use_memories=value.use_memories,
            )

        @router.put(
            "/{session_id}/memory-settings",
            response_model=SessionMemorySettingsResponse,
        )
        async def update_session_memory_settings(
            session_id: str,
            request: SessionMemorySettingsRequest,
            settings: SessionMemorySettingsService = Depends(memory_settings),
        ) -> SessionMemorySettingsResponse:
            try:
                value = await settings.update(
                    session_id,
                    use_memories=request.use_memories,
                )
            except LookupError as exc:
                detail = str(exc)
                if detail not in {"session_not_found"}:
                    detail = "session_not_found"
                raise HTTPException(status_code=404, detail=detail) from exc
            return SessionMemorySettingsResponse(
                session_id=value.session_id,
                use_memories=value.use_memories,
            )

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
        return [MessageResponse.from_application(value) for value in values]

    @router.get("/{session_id}/runs", response_model=list[RunSummaryResponse])
    async def list_session_runs(
        session_id: str,
        runs: RunQueryService = Depends(run_query),
    ) -> list[RunSummaryResponse]:
        try:
            values = await runs.list_for_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return [RunSummaryResponse.from_application(value) for value in values]

    @router.get("/{session_id}/runs/{run_id}/timeline", response_model=RunTimelineResponse)
    async def get_run_timeline(
        session_id: str,
        run_id: str,
        after: int = 0,
        limit: int = 200,
        runs: RunQueryService = Depends(run_query),
    ) -> RunTimelineResponse:
        try:
            value = await runs.timeline(session_id, run_id, after=after, limit=limit)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        return RunTimelineResponse.from_application(value)

    return router
