"""附件和文件任务 REST API。

提供文件上传、列表、详情、删除、重试，以及文件任务的查询和取消接口。
所有端点挂载在 ``/sessions/{session_id}`` 前缀下。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from athena.core.files.runtime import FileAccessError
from athena.runtime import runtime_from

router = APIRouter(prefix="/sessions/{session_id}", tags=["files"])


def _file_services(request: Request):
    """执行“文件服务”操作。

    参数：
        request (Request): 当前 HTTP 或 WebSocket 请求对象。

    返回值：
        Any: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    runtime = runtime_from(request)
    return runtime.file_runtime, runtime.file_worker


async def _ensure_session(session_id: str, request: Request):
    """执行“ensure session”操作。

    参数：
        session_id (str): 会话唯一标识。
        request (Request): 当前 HTTP 或 WebSocket 请求对象。

    返回值：
        Any: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    runtime = runtime_from(request)
    session = await runtime.db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return runtime.db


def _raise_file_http_error(exc: Exception) -> None:
    """将文件操作异常转换为 HTTP 异常。"""
    if isinstance(exc, (FileAccessError, FileNotFoundError)):
        raise HTTPException(
            status_code=404, detail=str(exc) or "Attachment not found"
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("/attachments", status_code=202)
async def upload_attachments(
    session_id: str, request: Request, files: list[UploadFile] = File(...)
) -> dict:
    """上传文件到指定会话（支持多文件）。

    处理流程：校验文件名 → 适配器选择 → 流式存储 → 创建附件记录 → 入队解析任务。

    返回值：
        包含 items 列表的字典，每项含 attachment 和 task 信息。
    """
    await _ensure_session(session_id, request)
    runtime, worker = _file_services(request)
    result = []
    for upload in files:
        filename = Path((upload.filename or "upload.bin").replace("\\", "/")).name
        if not filename or "\x00" in filename:
            await upload.close()
            raise HTTPException(status_code=400, detail="Invalid filename")
        try:
            try:
                runtime.adapter_registry.select(filename, upload.content_type or "")
            except ValueError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc

            async def chunks():
                """执行“chunks”操作。

                返回值：
                    Any: 操作结果；具体语义由调用场景决定。

                异常：
                    Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
                """
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

            try:
                stored = await runtime.storage.save_stream(chunks())
            except ValueError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            attachment = await runtime.repository.create_attachment(
                session_id=session_id,
                filename=filename,
                mime_type=upload.content_type or "application/octet-stream",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.storage_key,
            )
            task = await worker.enqueue_parse(session_id, attachment.id)
            attachment = (
                await runtime.repository.get_attachment(attachment.id, session_id)
                or attachment
            )
            result.append(
                {
                    "attachment": runtime.public_attachment(attachment),
                    "task": task.model_dump(mode="json"),
                }
            )
        finally:
            await upload.close()
    return {"items": result}


@router.get("/attachments")
async def list_attachments(session_id: str, request: Request) -> list[dict]:
    """列出会话的所有附件。"""
    await _ensure_session(session_id, request)
    runtime, _ = _file_services(request)
    return await runtime.list_files(session_id)


@router.get("/attachment-types")
async def get_supported_attachment_types(
    session_id: str, request: Request
) -> dict[str, list[str]]:
    """返回当前文件适配器支持选择的扩展名。"""
    await _ensure_session(session_id, request)
    runtime, _ = _file_services(request)
    return {"extensions": runtime.adapter_registry.supported_extensions()}


@router.get("/attachments/{file_id}")
async def get_attachment(session_id: str, file_id: str, request: Request) -> dict:
    """获取附件详情（含元数据）。"""
    await _ensure_session(session_id, request)
    runtime, _ = _file_services(request)
    try:
        return await runtime.get_file_info(session_id, file_id)
    except Exception as exc:
        _raise_file_http_error(exc)
        raise


@router.delete("/attachments/{file_id}")
async def delete_attachment(
    session_id: str, file_id: str, request: Request
) -> dict[str, str]:
    """软删除附件并清理关联数据（分块、产物、代码索引等）。"""
    await _ensure_session(session_id, request)
    runtime, worker = _file_services(request)
    deleted = await runtime.repository.soft_delete_attachment(file_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Attachment not found")
    await worker.resume_continuations(file_id)
    await runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "file_id": file_id}


@router.post("/attachments/{file_id}/retry", status_code=202)
async def retry_attachment(session_id: str, file_id: str, request: Request) -> dict:
    """重试失败的附件处理（重新入队解析任务）。"""
    await _ensure_session(session_id, request)
    runtime, worker = _file_services(request)
    try:
        attachment = await runtime.require_attachment(session_id, file_id)
        task = await worker.enqueue_parse(session_id, attachment.id)
        attachment = await runtime.require_attachment(session_id, file_id)
        return {
            "attachment": runtime.public_attachment(attachment),
            "task": task.model_dump(mode="json"),
        }
    except Exception as exc:
        _raise_file_http_error(exc)
        raise


@router.get("/file-tasks/{task_id}")
async def get_file_task(session_id: str, task_id: str, request: Request) -> dict:
    """查询文件处理任务状态。"""
    await _ensure_session(session_id, request)
    runtime, _ = _file_services(request)
    task = await runtime.repository.get_task(task_id, session_id)
    if task is None:
        raise HTTPException(status_code=404, detail="File task not found")
    return task.model_dump(mode="json")


@router.post("/file-tasks/{task_id}/cancel")
async def cancel_file_task(
    session_id: str, task_id: str, request: Request
) -> dict[str, str]:
    """取消文件处理任务（标记任务和附件为失败/取消状态）。"""
    await _ensure_session(session_id, request)
    runtime, worker = _file_services(request)
    task = await runtime.repository.get_task(task_id, session_id)
    if task is None:
        raise HTTPException(status_code=404, detail="File task not found")
    await runtime.repository.update_task(
        task_id,
        status="cancelled",
        stage="cancelled",
        completed_at=datetime.now().isoformat(),
    )
    await runtime.repository.update_attachment(
        task.attachment_id, status="failed", error_message="文件处理任务已取消"
    )
    await worker.resume_continuations(task.attachment_id)
    return {"status": "cancelled", "task_id": task_id}
