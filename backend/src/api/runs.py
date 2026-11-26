"""Run 管理 HTTP 接口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.types import Message, Send

from api.converter.run import (
    events_to_response,
    run_details_to_response,
    run_list_to_response,
    run_to_response,
    tool_call_to_response,
    tool_calls_to_response,
)
from api.dto.run import (
    RunAnswerRequest,
    RunEventListResponse,
    RunListResponse,
    RunResponse,
    RunRetryRequest,
    RunSubmitRequest,
    RunToolApprovalRequest,
    RunToolCallDetailResponse,
    RunToolCallListResponse,
)
from api.security import DEFAULT_COOKIE_NAME, CurrentAuth
from app.run.service import InvalidRunInputError, RunService
from app.run.stream import RunStreamService
from domain.run.model import MAX_RUN_SSE_EVENT_BYTES, MAX_RUN_SSE_SNAPSHOT_BYTES
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunKnowledgeBaseNotFoundError,
    RunNotFoundError,
    RunStateError,
    ToolRetryConfirmationRequiredError,
)
from domain.session import SessionNotFoundError
from domain.tool import MAX_TOOL_CALL_ID_LENGTH, ToolApprovalDecision


class _RunEventSourceResponse(StreamingResponse):
    """对每次 ASGI 写入设置上限，慢客户端不占住流服务资源。"""

    async def stream_response(self, send: Send) -> None:
        async def bounded_send(message: Message) -> None:
            async with asyncio.timeout(1.0):
                await send(message)

        await super().stream_response(bounded_send)


def _not_found(error: Exception) -> HTTPException:
    if isinstance(error, RunKnowledgeBaseNotFoundError):
        detail = "knowledge_base_not_found"
    elif isinstance(error, RunNotFoundError):
        detail = "run_not_found"
    else:
        detail = "session_not_found"
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=detail,
    )


def _conflict(error: Exception) -> HTTPException:
    detail = str(error) or "run_conflict"
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def build_run_router(
    service: RunService, stream_service: RunStreamService | None = None
) -> APIRouter:
    """绑定 Run 用例并创建业务路由。"""
    router = APIRouter(
        prefix="/api/v1/sessions/{session_id}/runs",
        tags=["runs"],
    )

    @router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def submit_run(
        request: RunSubmitRequest,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        response: Response,
    ) -> RunResponse:
        """原子提交一条文本任务。"""
        try:
            result = await service.submit(
                session_id,
                current_auth.user.id,
                request.request_id,
                request.text,
                skill_ids=tuple(request.skill_ids),
                input_file_ids=tuple(request.input_file_ids),
                knowledge_base_ids=(
                    None
                    if request.knowledge_base_ids is None
                    else tuple(request.knowledge_base_ids)
                ),
                model_profile_id=request.model_profile_id,
                auth_session_id=current_auth.session_id,
            )
        except RunKnowledgeBaseNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="knowledge_base_not_found",
            ) from error
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except (RunConflictError, RunStateError) as error:
            raise _conflict(error) from error
        except (InvalidRunInputError, RunInputResourceError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        response.headers["Location"] = f"/api/v1/sessions/{session_id}/runs/{result.run.id}"
        return run_to_response(result.run, replayed=result.replayed)

    @router.get("", response_model=RunListResponse)
    async def list_runs(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> RunListResponse:
        """游标分页读取当前用户可见的 Run。"""
        try:
            return run_list_to_response(
                await service.list(session_id, current_auth.user.id, cursor=cursor, limit=limit)
            )
        except SessionNotFoundError as error:
            raise _not_found(error) from error
        except (InvalidRunInputError, RunInputResourceError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{run_id}", response_model=RunResponse)
    async def get_run(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> RunResponse:
        """读取 Run 状态和当前待回答请求。"""
        try:
            return run_details_to_response(
                await service.get(session_id, current_auth.user.id, run_id)
            )
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error

    @router.post("/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> RunResponse:
        """请求取消并返回当前状态；重复取消幂等。"""
        try:
            return run_to_response(await service.cancel(session_id, current_auth.user.id, run_id))
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except (RunConflictError, RunStateError) as error:
            raise _conflict(error) from error

    @router.post("/{run_id}/retry", response_model=RunResponse)
    async def retry_run(
        request: RunRetryRequest,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> RunResponse:
        """仅为最新失败 Run 创建从头执行的新尝试；可能重复已成功操作。"""
        try:
            return run_to_response(
                await service.retry(
                    session_id,
                    current_auth.user.id,
                    run_id,
                    request.request_id,
                    confirm_unknown=request.confirm_unknown,
                    auth_session_id=current_auth.session_id,
                )
            )
        except (RunNotFoundError, SessionNotFoundError, RunKnowledgeBaseNotFoundError) as error:
            raise _not_found(error) from error
        except ToolRetryConfirmationRequiredError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="tool_retry_confirmation_required",
            ) from error
        except (RunConflictError, RunStateError) as error:
            raise _conflict(error) from error
        except (InvalidRunInputError, RunInputResourceError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post(
        "/{run_id}/input-requests/{input_request_id}/answer",
        response_model=RunResponse,
    )
    async def answer_input(
        request: RunAnswerRequest,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        input_request_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
    ) -> RunResponse:
        """回答指定输入请求；命令幂等键与输入请求 ID 分开传递。"""
        try:
            run = await service.answer(
                session_id,
                current_auth.user.id,
                run_id,
                input_request_id,
                request.request_id,
                request.text,
                input_file_ids=tuple(request.input_file_ids),
                auth_session_id=current_auth.session_id,
            )
            return run_to_response(run)
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except (RunConflictError, RunStateError) as error:
            raise _conflict(error) from error
        except (InvalidRunInputError, RunInputResourceError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{run_id}/events", response_model=RunEventListResponse)
    async def list_run_events(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> RunEventListResponse:
        """按 Run 内事件序号正序读取一页。"""
        try:
            events = await service.events(
                session_id,
                current_auth.user.id,
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return events_to_response(events, limit)
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except InvalidRunInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{run_id}/tool-calls", response_model=RunToolCallListResponse)
    async def list_tool_calls(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        after_ordinal: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> RunToolCallListResponse:
        """Cursor through ordinal-ordered tool calls without visible arguments."""
        try:
            return tool_calls_to_response(
                await service.list_tool_calls(
                    session_id,
                    current_auth.user.id,
                    run_id,
                    after_ordinal=after_ordinal,
                    limit=limit,
                )
            )
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except InvalidRunInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get(
        "/{run_id}/tool-calls/{call_id}",
        response_model=RunToolCallDetailResponse,
    )
    async def get_tool_call(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        call_id: Annotated[str, Path(min_length=1, max_length=MAX_TOOL_CALL_ID_LENGTH)],
        current_auth: CurrentAuth,
    ) -> RunToolCallDetailResponse:
        """Read bounded call arguments and normalized result after authorization."""
        try:
            return tool_call_to_response(
                await service.get_tool_call(
                    session_id,
                    current_auth.user.id,
                    run_id,
                    call_id,
                )
            )
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except InvalidRunInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post(
        "/{run_id}/tool-calls/{call_id}/approval",
        response_model=RunResponse,
    )
    async def decide_tool_approval(
        request: RunToolApprovalRequest,
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        call_id: Annotated[str, Path(min_length=1, max_length=MAX_TOOL_CALL_ID_LENGTH)],
        current_auth: CurrentAuth,
    ) -> RunResponse:
        """Approve or reject one persisted call without resubmitting arguments."""
        try:
            details = await service.decide_tool_approval(
                session_id,
                current_auth.user.id,
                run_id,
                call_id,
                request.request_id,
                ToolApprovalDecision(request.decision),
                auth_session_id=current_auth.session_id,
            )
            return run_details_to_response(details)
        except (RunNotFoundError, SessionNotFoundError) as error:
            raise _not_found(error) from error
        except (RunConflictError, RunStateError) as error:
            raise _conflict(error) from error
        except InvalidRunInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    if stream_service is not None:

        @router.get(
            "/{run_id}/stream",
            response_class=StreamingResponse,
            responses={
                200: {
                    "description": "Server-Sent Events stream for run execution",
                    "content": {
                        "text/event-stream": {"schema": {"type": "string"}},
                    },
                }
            },
        )
        async def stream_run(
            request: Request,
            session_id: Annotated[str, Path(min_length=1, max_length=64)],
            run_id: Annotated[str, Path(min_length=1, max_length=64)],
            current_auth: CurrentAuth,
        ) -> StreamingResponse:
            """以 SSE 推送快照、临时文本和数据库状态事件。"""
            if not await stream_service.authorize(session_id, current_auth.user.id, run_id):
                raise HTTPException(status_code=404, detail="run_not_found")
            authenticator = getattr(request.app.state, "authenticator", None)
            cookie_name = getattr(request.app.state, "auth_cookie_name", DEFAULT_COOKIE_NAME)
            token = request.cookies.get(cookie_name)
            if authenticator is None or token is None:
                raise HTTPException(status_code=401, detail="not_authenticated")

            async def body() -> AsyncIterator[bytes]:
                async for event in stream_service.events(
                    session_id,
                    current_auth.user.id,
                    run_id,
                    token,
                    last_event_id=request.headers.get("last-event-id"),
                ):
                    data = event.model_dump_json(exclude_none=True)
                    max_bytes = (
                        MAX_RUN_SSE_SNAPSHOT_BYTES
                        if event.type == "snapshot"
                        else MAX_RUN_SSE_EVENT_BYTES
                    )
                    # 单次 UTF-8 编码同时完成字节上限校验与响应体输出，消除 Starlette 二次编码开销。
                    wire_bytes = (
                        f"event: {event.type}\nid: {event.stream_sequence}\ndata: {data}\n\n"
                    ).encode()
                    if len(wire_bytes) > max_bytes:
                        raise RuntimeError("run_stream_event_exceeds_size_limit")
                    yield wire_bytes

            return _RunEventSourceResponse(
                body(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )

    return router


__all__ = ["build_run_router"]
