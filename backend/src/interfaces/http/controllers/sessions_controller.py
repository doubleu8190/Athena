"""会话 Controller 的新实现。

该 Router 不自行查数据库。应用启动时通过 ``session_service`` 注入
``SessionService``，因此 HTTP 层可以独立测试。
"""

from __future__ import annotations


from fastapi import APIRouter, Header, HTTPException, Query, Response

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
    RunSummaryResponse,
    SessionMessageHistoryResponse,
)
from interfaces.http.dto.runs import RunTimelineResponse


def build_sessions_router(
    session_service: SessionService,
    *,
    message_query_service: SessionMessageQueryService | None = None,
    run_query_service: RunQueryService | None = None,
    memory_settings_service: SessionMemorySettingsService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["sessions"])

    @router.post("", response_model=SessionResponse)
    async def create_session(
        request: CreateSessionRequest,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> SessionResponse:
        try:
            value = await session_service.create(
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
    ) -> list[SessionResponse]:
        return [
            SessionResponse.from_application(item)
            for item in await session_service.list_sessions(project_id=project_id)
        ]

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: str,
    ) -> SessionResponse:
        try:
            return SessionResponse.from_application(await session_service.get(session_id))
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc

    @router.patch("/{session_id}", response_model=SessionResponse)
    async def rename_session(
        session_id: str,
        request: UpdateSessionRequest,
    ) -> SessionResponse:
        try:
            updated = await session_service.rename(session_id, request.title)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except ValueError as exc:
            detail = "title_too_long" if str(exc) == "title_too_long" else "title_empty"
            raise HTTPException(status_code=400, detail=detail) from exc
        return SessionResponse.from_application(updated)

    @router.delete("/{session_id}", response_model=SessionDeletionResponse)
    async def delete_session(
        session_id: str,
    ) -> SessionDeletionResponse:
        try:
            await session_service.delete(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return SessionDeletionResponse(status="deleted", session_id=session_id)

    if memory_settings_service is not None:
        @router.get(
            "/{session_id}/memory-settings",
            response_model=SessionMemorySettingsResponse,
        )
        async def get_session_memory_settings(
            session_id: str,
        ) -> SessionMemorySettingsResponse:
            try:
                value = await memory_settings_service.get(session_id)
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
        ) -> SessionMemorySettingsResponse:
            try:
                value = await memory_settings_service.update(
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

    @router.get("/{session_id}/messages", response_model=SessionMessageHistoryResponse)
    async def list_session_messages(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None),
    ) -> SessionMessageHistoryResponse:
        try:
            values = await message_query_service.list_messages(session_id, limit=limit, cursor=cursor)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_history_cursor") from exc
        return SessionMessageHistoryResponse.from_application(values)

    @router.get("/{session_id}/runs", response_model=list[RunSummaryResponse])
    async def list_session_runs(
        session_id: str,
    ) -> list[RunSummaryResponse]:
        try:
            values = await run_query_service.list_for_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return [RunSummaryResponse.from_application(value) for value in values]

    @router.get("/{session_id}/runs/{run_id}/timeline", response_model=RunTimelineResponse)
    async def get_run_timeline(
        session_id: str,
        run_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> RunTimelineResponse:
        try:
            value = await run_query_service.timeline(session_id, run_id, after=after, limit=limit)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        return RunTimelineResponse.from_application(value)

    return router
