"""运行命令和命令状态 Controller。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from application.runs import (
    CommandNotFoundError,
    CommandRejectedError,
    RunNotFoundError,
    RunCommandService,
)
from application.sessions import SessionNotFoundError
from interfaces.http.dto.runs import (
    CancelCommandResponse,
    CommandStatusResponse,
    SubmitRunRequest,
    SubmitRunResponse,
)


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
        return SubmitRunResponse.from_domain(value)

    @router.post("/sessions/{session_id}/cancel", status_code=202)
    async def cancel_session(
        session_id: str,
        run_id: str | None = None,
        commands: RunCommandService = Depends(service),
    ) -> CancelCommandResponse:
        try:
            value = await commands.cancel_session(session_id, run_id=run_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return CancelCommandResponse.from_domain(value)

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: str,
        commands: RunCommandService = Depends(service),
    ) -> CancelCommandResponse:
        try:
            value = await commands.cancel_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        return CancelCommandResponse.from_domain(value)

    @router.get("/commands/{command_id}")
    async def get_command(
        command_id: str,
        commands: RunCommandService = Depends(service),
    ) -> CommandStatusResponse:
        try:
            value = await commands.get_command(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(status_code=404, detail="command_not_found") from exc
        return CommandStatusResponse.from_domain(value)

    return router


__all__ = ["build_runs_router"]
