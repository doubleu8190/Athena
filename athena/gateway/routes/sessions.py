"""会话管理路由 — 创建/列出/查询/删除会话、发送消息、恢复中断会话。

提供会话的完整 CRUD 操作，以及消息发送、执行步骤查询、工具调用记录查询、
会话停止和恢复等端点。所有端点挂载在 ``/sessions`` 前缀下。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel

from athena.infrastructure.sqlite.database import Database
from athena.models import CommandPayload, Message, Session
from athena.utils.ids import generate_session_id
from athena.utils.logging import get_logger
from athena.container import RuntimeContainer, runtime_from
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.statuses import AgentCommandStatus
from athena.gateway.routes.schemas import (
    CancelCommandResponse,
    ControlCommandResponse,
    RunSummaryResponse,
    SessionDeletionResponse,
    SubmitRunResponse,
)
from athena.utils.ids import generate_time_id

logger = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    """创建会话请求体。"""

    title: str = "New Session"


class SendMessageRequest(BaseModel):
    """发送消息请求体。"""

    message: str
    command_id: str


@dataclass
class ParsedSubmitRun:
    """已解析的消息提交请求及本次请求产生的附件。"""

    message: str
    command_id: str
    attachment_ids: list[str] = field(default_factory=list)
    uploaded_ids: list[str] = field(default_factory=list)


def _is_upload_file(value: object) -> TypeGuard[UploadFile]:
    """判断 multipart 表单值是否具备上传文件接口。"""
    return isinstance(value, UploadFile) or (
        hasattr(value, "filename")
        and hasattr(value, "content_type")
        and hasattr(value, "read")
        and hasattr(value, "close")
    )


async def _db_for(request: Request) -> Database:
    """从请求上下文获取数据库实例。"""
    return runtime_from(request).db


async def _cleanup_uploaded_attachments(
    runtime: RuntimeContainer, session_id: str, uploaded_ids: list[str]
) -> None:
    """回滚本次请求已创建的附件及其无引用 blob。"""
    for attachment_id in uploaded_ids:
        await runtime.file_runtime.repository.soft_delete_attachment(
            attachment_id, session_id
        )
    if uploaded_ids:
        await runtime.file_runtime.cleanup_unreferenced_blobs()


async def _parse_json_run_request(request: Request) -> ParsedSubmitRun:
    """解析 JSON 消息提交请求。"""
    try:
        req = SendMessageRequest.model_validate(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ParsedSubmitRun(message=req.message, command_id=req.command_id)


async def _parse_multipart_run_request(
    session_id: str,
    request: Request,
    runtime: RuntimeContainer,
    message_id: str,
) -> ParsedSubmitRun:
    """解析 multipart 消息提交请求并保存附件。"""
    form = await request.form()
    message = str(form.get("message") or "")
    command_id = str(form.get("command_id") or "")
    upload_files = [
        item
        for item in form.getlist("files")
        if _is_upload_file(item)
    ]
    if not command_id:
        for file in upload_files:
            await file.close()
        raise HTTPException(status_code=422, detail="command_id is required")

    attachment_ids: list[str] = []
    uploaded_ids: list[str] = []
    for file in upload_files:
        try:
            filename = Path((file.filename or "upload.bin").replace("\\", "/")).name
            if not filename or "\x00" in filename:
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.INVALID_FILENAME
                )
            try:
                # 判断是否是支持的文件类型，若不支持则抛出 ValueError
                runtime.file_runtime.adapter_registry.select(
                    filename, file.content_type or ""
                )
            except ValueError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc

            async def chunks():
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

            try:
                stored = await runtime.file_runtime.storage.save_stream(chunks())
            except ValueError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            attachment = await runtime.file_runtime.repository.create_attachment(
                session_id=session_id,
                message_id=message_id,
                filename=filename,
                mime_type=file.content_type or "application/octet-stream",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.storage_key,
            )
            attachment_ids.append(attachment.id)
            uploaded_ids.append(attachment.id)
        except Exception:
            await _cleanup_uploaded_attachments(runtime, session_id, uploaded_ids)
            raise
        finally:
            await file.close()

    return ParsedSubmitRun(
        message=message,
        command_id=command_id,
        attachment_ids=attachment_ids,
        uploaded_ids=uploaded_ids,
    )


@router.post("")
async def create_session(req: CreateSessionRequest, request: Request) -> Session:
    """创建新会话."""
    db = await _db_for(request)
    session_id = generate_session_id()
    session = await db.sessions.create(session_id, title=req.title)
    return session


@router.get("")
async def list_sessions(request: Request) -> list[Session]:
    """列出所有会话."""
    db = await _db_for(request)
    return await db.sessions.list_all()


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> Session:
    """获取会话详情."""
    db = await _db_for(request)
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    return session


class UpdateSessionRequest(BaseModel):
    """表示 UpdateSessionRequest 组件，封装相关状态和行为。"""

    title: str


@router.patch("/{session_id}")
async def update_session(
    session_id: str, req: UpdateSessionRequest, request: Request
) -> Session:
    """重命名会话."""
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail=ErrorDetail.TITLE_EMPTY)
    db = await _db_for(request)
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    await db.sessions.update(session_id, title=title)
    updated = await db.sessions.get(session_id)
    assert updated is not None
    return updated


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> SessionDeletionResponse:
    """删除会话及其所有关联数据."""
    db = await _db_for(request)
    await db.files.soft_delete_session_attachments(session_id)
    await runtime_from(request).file_runtime.cleanup_unreferenced_blobs()
    await db.sessions.delete(session_id)
    return SessionDeletionResponse(status="deleted", session_id=session_id)


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str, request: Request, limit: int | None = None
) -> list[Message]:
    """获取会话消息列表."""
    db = await _db_for(request)
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    return await db.messages.get_by_session(session_id, limit=limit)


@router.get("/{session_id}/runs")
async def get_runs(session_id: str, request: Request) -> list[RunSummaryResponse]:
    """列出会话的运行记录及暂停、取消标志。

    参数:
        session_id (str): 会话 ID。
        request (Request): 当前请求，用于获取运行时容器。
    返回值:
        list[RunSummaryResponse]: 按创建时间排列的运行记录。
    异常:
        HTTP异常: 会话不存在时返回 404。
    """
    runtime = runtime_from(request)
    if not await runtime.db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    store = runtime.agent_store
    return [
        RunSummaryResponse(
            run_id=row.run_id,
            session_id=row.session_id,
            status=row.status,
            created_by_command_id=row.created_by_command_id,
            pause_requested=bool(row.pause_requested),
            cancel_requested=bool(row.cancel_requested),
            error=row.error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in await store.list_runs_for_session(session_id)
    ]


@router.post("/{session_id}/runs", status_code=202)
async def submit_run(session_id: str, request: Request) -> SubmitRunResponse:
    """提交异步消息命令，交由 Runtime 消费者执行。

    参数:
        session_id (str): 目标会话 ID。
        请求体：JSON 请求或 multipart 表单，包含消息、文件和命令 ID。
        request (Request): 当前请求，用于获取运行时容器。
    返回值:
        SubmitRunResponse: 命令 ID、运行 ID、待处理状态和幂等去重标志。
    异常:
        HTTP异常: 会话不存在、命令冲突或会话已有不可并行运行时抛出。
    """
    runtime = runtime_from(request)
    if not await runtime.db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)

    message_id = generate_time_id()
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/"):
        parsed = await _parse_multipart_run_request(
            session_id, request, runtime, message_id
        )
    else:
        parsed = await _parse_json_run_request(request)

    message = parsed.message
    command_id = parsed.command_id
    attachment_ids = parsed.attachment_ids
    uploaded_ids = parsed.uploaded_ids

    command = Command(
        command_id=command_id,
        command_type=CommandType.MESSAGE_SUBMIT,
        session_id=session_id,
        run_id=generate_time_id(),
        payload=CommandPayload.model_validate(
            {
                "message": message,
                "message_id": message_id,
                "attachment_ids": attachment_ids,
            }
        ),
    )
    try:
        inserted = await runtime.agent_store.enqueue_command(command)
    except ValueError as exc:
        await _cleanup_uploaded_attachments(runtime, session_id, uploaded_ids)
        detail = str(exc)
        try:
            error_detail = ErrorDetail(detail)
        except ValueError:
            error_detail = None
        status = (
            409
            if error_detail
            in {
                ErrorDetail.COMMAND_ID_CONFLICT,
                ErrorDetail.RUN_ID_CONFLICT,
                ErrorDetail.SESSION_BUSY,
            }
            else 400
        )
        raise HTTPException(status_code=status, detail=error_detail or detail) from exc

    if not inserted:
        existing = await runtime.agent_store.get_command(command.command_id)
        if existing is not None:
            stored_payload = CommandPayload.model_validate_json(existing.payload_json)
            message_id = stored_payload.message_id or message_id
            attachment_ids = stored_payload.attachment_ids

    return SubmitRunResponse(
        command_id=command.command_id,
        run_id=command.run_id,
        message_id=message_id,
        attachment_ids=attachment_ids,
        status=AgentCommandStatus.PENDING,
        deduplicated=not inserted,
    )


async def _enqueue_control(
    session_id: str, command_type: CommandType, request: Request
) -> ControlCommandResponse:
    """将暂停、恢复或其他控制命令写入命令队列。

    参数:
        session_id (str): 目标会话 ID。
        command_type (CommandType): 控制命令类型。
        request (Request): 当前请求，用于获取运行时容器。
    返回值:
        ControlCommandResponse: 新命令 ID 及其待处理状态。
    异常:
        HTTP异常: 会话不存在时返回 404；入队失败时传播对应错误。
    """
    runtime = runtime_from(request)
    if not await runtime.db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    store = runtime.agent_store
    active = await store.get_active_run(session_id)
    command = Command(
        command_id=f"cmd_{generate_time_id()}",
        command_type=command_type,
        session_id=session_id,
        run_id=active.run_id if active else None,
    )
    await store.enqueue_command(command)
    return ControlCommandResponse(
        command_id=command.command_id,
        status=AgentCommandStatus.PENDING,
    )


@router.post("/{session_id}/pause", status_code=202)
async def pause_run(session_id: str, request: Request) -> ControlCommandResponse:
    """提交暂停会话的控制命令。"""
    return await _enqueue_control(session_id, CommandType.RUN_PAUSE, request)


@router.post("/{session_id}/resume", status_code=202)
async def resume_run(session_id: str, request: Request) -> ControlCommandResponse:
    """提交恢复会话的控制命令。"""
    return await _enqueue_control(session_id, CommandType.RUN_RESUME, request)


@router.post("/{session_id}/cancel", status_code=202)
async def cancel_run(
    session_id: str, request: Request, run_id: str | None = None
) -> CancelCommandResponse:
    """提交取消会话运行的控制命令。

    参数:
        session_id (str): 目标会话 ID。
        request (Request): 当前请求，用于获取运行时容器。
        run_id (str | None): 可选目标运行 ID；为空时由命令关联当前请求上下文。
    返回值:
        CancelCommandResponse: 新命令 ID、目标运行 ID 和待处理状态。
    异常:
        HTTP异常: 会话不存在或命令入队失败时抛出。
    """
    runtime = runtime_from(request)
    if not await runtime.db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)
    command = Command(
        command_id=f"cmd_{generate_time_id()}",
        command_type=CommandType.RUN_CANCEL,
        session_id=session_id,
        run_id=run_id,
    )
    await runtime.agent_store.enqueue_command(command)
    return CancelCommandResponse(
        command_id=command.command_id,
        run_id=run_id,
        status=AgentCommandStatus.PENDING,
    )
