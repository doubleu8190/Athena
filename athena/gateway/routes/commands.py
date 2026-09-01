from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException
from athena.runtime import runtime_from
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.statuses import AgentCommandStatus
from athena.utils.ids import generate_time_id
from athena.gateway.routes.schemas import CancelCommandResponse, CommandStatusResponse
from athena.models.json_models import JsonObject
from athena.infrastructure.sqlite.repositories import _json_loads_model

router = APIRouter(prefix="/commands", tags=["commands"])
run_router = APIRouter(prefix="/runs", tags=["runs"])


@run_router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, request: Request) -> CancelCommandResponse:
    """为指定运行提交异步取消命令。

    参数:
        run_id (str): 要取消的运行 ID。
        request (Request): 当前请求，用于获取运行时容器。
    返回值:
        CancelCommandResponse: 新命令 ID、运行 ID 和待处理状态。
    异常:
        HTTP异常: 运行不存在时返回 404。
    """
    runtime = runtime_from(request)
    store = runtime.agent_store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.RUN_NOT_FOUND)
    command = Command(
        command_id=f"cmd_{generate_time_id()}",
        command_type=CommandType.RUN_CANCEL,
        session_id=run.session_id,
        run_id=run_id,
    )
    await store.enqueue(command)
    return CancelCommandResponse(
        command_id=command.command_id,
        run_id=run_id,
        status=AgentCommandStatus.PENDING,
    )


__all__ = ["router", "run_router"]


@router.get("/{command_id}")
async def get_command(command_id: str, request: Request) -> CommandStatusResponse:
    """查询命令的处理状态和结果。

    参数:
        command_id (str): 命令 ID。
        request (Request): 当前请求，用于获取 Agent Store。
    返回值:
        CommandStatusResponse: 命令状态、尝试次数、结果和错误信息。
    异常:
        HTTP异常: 命令不存在时返回 404。
    """
    store = runtime_from(request).agent_store
    row = await store.get_command(command_id)
    if row is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.COMMAND_NOT_FOUND)
    return CommandStatusResponse(
        command_id=row.command_id,
        session_id=row.session_id,
        run_id=row.run_id,
        command_type=row.command_type,
        status=row.status,
        attempt=row.attempt,
        result=(
            _json_loads_model(row.result_json, JsonObject, JsonObject()).model_dump(
                mode="json", exclude_none=True
            )
            if row.result_json
            else None
        ),
        error=(
            _json_loads_model(row.error_json, JsonObject, JsonObject()).model_dump(
                mode="json", exclude_none=True
            )
            if row.error_json
            else None
        ),
    )
