"""Run submission, stop and query HTTP routes."""

from __future__ import annotations


from fastapi import APIRouter, Header, HTTPException, Query, Response

from application.runs import RunNotFoundError, RunQueryService, RunRejectedError, RunService
from application.observability import RunObservabilityService
from application.sessions import SessionNotFoundError
from interfaces.http.dto.runs import (
    RunListResponse,
    RunTraceResponse,
    RunTraceSpanResponse,
    StopRunRequest,
    StopRunResponse,
    SubmitRunRequest,
    SubmitRunResponse,
)
from interfaces.http.dto.sessions import RunSummaryResponse


def build_run_query_router(
    run_query_service: RunQueryService,
    observability_service: RunObservabilityService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.get("/runs", response_model=RunListResponse)
    async def list_runs(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        session_id: str | None = None,
        terminal_only: bool = Query(False),
    ) -> RunListResponse:
        try:
            value = await run_query_service.list_recent(
                limit=limit,
                offset=offset,
                session_id=session_id,
                terminal_only=terminal_only,
            )
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
    ) -> RunSummaryResponse:
        try:
            return RunSummaryResponse.from_application(await run_query_service.get(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc

    @router.get("/runs/{run_id}/trace", response_model=RunTraceResponse)
    async def get_run_trace(run_id: str) -> RunTraceResponse:
        """读取已完成 Run 的 Trace；活动 Run 不提供实时链路。"""
        if observability_service is None:
            raise HTTPException(status_code=503, detail="run_trace_unavailable")
        try:
            run = await run_query_service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        if run.status not in {"completed", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="run_not_terminal")
        spans = await observability_service.list_run_spans(run_id)
        return RunTraceResponse(
            run_id=run.run_id,
            status=run.status,
            spans=[
                RunTraceSpanResponse.from_application(value)
                for value in spans
            ],
        )

    return router


def build_runs_router(
    run_service: RunService,
) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.post("/runs", status_code=202, response_model=SubmitRunResponse)
    async def submit_run(
        request: SubmitRunRequest,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> SubmitRunResponse:
        try:
            value = await run_service.submit(
                request.session_id,
                project_id=request.project_id,
                request_id=idempotency_key,
                message=request.message,
                attachment_ids=request.attachment_ids,
            )
        except RunRejectedError as exc:
            status = 404 if exc.code in {"session_not_found", "project_not_found"} else 409 if exc.code in {
                "idempotency_conflict", "session_project_mismatch", "active_run_changed",
                "session_run_in_progress", "session_run_queued",
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
    ) -> StopRunResponse:
        try:
            value = await run_service.stop_session(
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
