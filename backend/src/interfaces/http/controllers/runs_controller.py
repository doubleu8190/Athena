"""Run submission, stop and query HTTP routes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from application.runs import RunNotFoundError, RunQueryService, RunRejectedError, RunService
from application.sessions import SessionNotFoundError
from interfaces.http.dto.runs import (
    RunListResponse,
    StopRunRequest,
    StopRunResponse,
    SubmitRunRequest,
    SubmitRunResponse,
)
from interfaces.http.dto.sessions import RunSummaryResponse


def build_run_query_router(
    query_service_factory: Callable[[], RunQueryService],
) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.get("/runs", response_model=RunListResponse)
    async def list_runs(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        session_id: str | None = None,
        runs: RunQueryService = Depends(query_service_factory),
    ) -> RunListResponse:
        try:
            value = await runs.list_recent(limit=limit, offset=offset, session_id=session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return RunListResponse(
            items=[RunSummaryResponse.from_application(item) for item in value.items],
            total=value.total,
            has_more=value.has_more,
        )

    @router.get("/runs/{run_id}", response_model=RunSummaryResponse)
    async def get_run(
        run_id: str,
        runs: RunQueryService = Depends(query_service_factory),
    ) -> RunSummaryResponse:
        try:
            return RunSummaryResponse.from_application(await runs.get(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc

    return router


def build_runs_router(
    run_service_factory: Callable[[], RunService],
) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.post("/runs", status_code=202, response_model=SubmitRunResponse)
    async def submit_run(
        request: SubmitRunRequest,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        runs: RunService = Depends(run_service_factory),
    ) -> SubmitRunResponse:
        try:
            value = await runs.submit(
                request.session_id,
                project_id=request.project_id,
                request_id=idempotency_key,
                message=request.message,
                attachment_ids=request.attachment_ids,
            )
        except RunRejectedError as exc:
            status = 404 if exc.code in {"session_not_found", "project_not_found"} else 409 if exc.code in {
                "idempotency_conflict", "session_project_mismatch", "active_run_changed"
            } else 400
            raise HTTPException(status_code=status, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if value.replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return SubmitRunResponse.from_application(value)

    @router.post(
        "/sessions/{session_id}/stop",
        status_code=202,
        response_model=StopRunResponse,
    )
    async def stop_session(
        session_id: str,
        request: StopRunRequest,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        runs: RunService = Depends(run_service_factory),
    ) -> StopRunResponse:
        try:
            value = await runs.stop_session(
                session_id,
                request_id=idempotency_key,
                expected_run_id=request.expected_run_id,
                displayed_session_seq=request.displayed_session_seq,
            )
        except RunRejectedError as exc:
            status = 404 if exc.code == "session_not_found" else 409 if exc.code == "active_run_changed" else 422
            raise HTTPException(status_code=status, detail=exc.code) from exc
        if value.replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return StopRunResponse.from_application(value)

    return router


__all__ = ["build_runs_router", "build_run_query_router"]
