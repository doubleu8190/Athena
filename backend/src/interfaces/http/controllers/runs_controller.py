"""运行命令和命令状态 Controller。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from application.runs import (
    CommandNotFoundError,
    CommandRejectedError,
    RunNotFoundError,
    RunCommandService,
    RunQueryService,
)
from application.sessions import SessionNotFoundError
from interfaces.http.dto.runs import (
    CancelCommandResponse,
    CommandStatusResponse,
    RunListResponse,
    SubmitRunRequest,
    SubmitRunResponse,
)
from interfaces.http.dto.sessions import RunSummaryResponse


def build_run_query_router(
    query_service_factory: Callable[[], RunQueryService],
) -> APIRouter:
    """创建运行历史路由。

    Args:
        query_service_factory: 返回运行查询用例的工厂。

    Returns:
        提供跨会话分页查询的路由。

    Raises:
        无预期业务异常。
    """
    router = APIRouter(tags=["runs"])

    @router.get("/runs", response_model=RunListResponse)
    async def list_runs(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        session_id: str | None = None,
        runs: RunQueryService = Depends(query_service_factory),
    ) -> RunListResponse:
        """返回运行历史。

        Args:
            limit: 每页条数，范围 1 到 200。
            offset: 非负分页偏移。
            session_id: 可选的会话 ID。
            runs: 注入的运行查询用例。

        Returns:
            运行摘要、总条数及分页标记。

        Raises:
            HTTPException: 指定会话不存在时返回 404。
        """
        try:
            value = await runs.list_recent(limit=limit, offset=offset, session_id=session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return RunListResponse(
            items=[RunSummaryResponse.from_application(item) for item in value.items],
            total=value.total, has_more=value.has_more,
        )

    return router


def build_runs_router(
    command_service_factory: Callable[[], RunCommandService],
) -> APIRouter:
    """注册运行提交、取消和命令状态路由。"""
    router = APIRouter(tags=["runs"])

    def service() -> RunCommandService:
        return command_service_factory()

    @router.post("/sessions/{session_id}/runs", status_code=202)
    async def submit_run(
        session_id: str,
        request: SubmitRunRequest,
        commands: RunCommandService = Depends(service),
    ) -> SubmitRunResponse:
        try:
            value = await commands.submit(
                session_id,
                command_id=request.command_id,
                message=request.message,
                message_id=request.message_id,
                attachment_ids=request.attachment_ids,
            )
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except CommandRejectedError as exc:
            status = 409 if exc.code in {
                "command_id_conflict",
                "run_id_conflict",
                "session_busy",
            } else 400
            raise HTTPException(status_code=status, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return SubmitRunResponse.from_application(value)

    @router.post("/sessions/{session_id}/cancel", status_code=202)
    async def cancel_session(
        session_id: str,
        run_id: str | None = None,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        commands: RunCommandService = Depends(service),
    ) -> CancelCommandResponse:
        try:
            value = await commands.cancel_session(
                session_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CancelCommandResponse.from_application(value)

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        commands: RunCommandService = Depends(service),
    ) -> CancelCommandResponse:
        try:
            value = await commands.cancel_run(
                run_id,
                idempotency_key=idempotency_key,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CancelCommandResponse.from_application(value)

    @router.get("/commands/{command_id}")
    async def get_command(
        command_id: str,
        commands: RunCommandService = Depends(service),
    ) -> CommandStatusResponse:
        try:
            value = await commands.get_command(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(status_code=404, detail="command_not_found") from exc
        return CommandStatusResponse.from_application(value)

    return router


__all__ = ["build_runs_router", "build_run_query_router"]
