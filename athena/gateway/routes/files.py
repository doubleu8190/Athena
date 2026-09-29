"""会话附件 REST API。

支持独立上传、查询和删除附件；消息提交只需要传入附件 ID。
所有端点挂载在 ``/sessions/{session_id}`` 前缀下。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from athena.core.files.attachment_serialization import attachment_to_payload
from athena.core.files.runtime import FileAccessError
from athena.contracts.errors import ErrorDetail
from athena.container import get_runtime_container
from athena.utils.logging import get_logger

router = APIRouter(prefix="/sessions/{session_id}", tags=["files"])
logger = get_logger(__name__)


def _get_file_runtime(request: Request):
    """从请求应用状态获取文件运行时。

    参数：
        request (Request): 当前 HTTP 请求对象。

    返回值：
        FileIntelligenceRuntime: 文件服务。

    异常：
        RuntimeError: 应用运行时未初始化。
    """
    return get_runtime_container(request).file_runtime


async def _require_session(session_id: str, request: Request) -> None:
    """校验会话存在。

    参数：
        session_id (str): 会话唯一标识。
        request (Request): 当前 HTTP 请求对象。

    返回值：
        None: 会话存在时正常返回。

    异常：
        HTTP异常: 会话不存在时返回 404。
    """
    runtime = get_runtime_container(request)
    session = await runtime.db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)


def _raise_file_http_error(exc: Exception) -> None:
    """将文件操作异常转换为 HTTP 异常。"""
    if isinstance(exc, (FileAccessError, FileNotFoundError)):
        raise HTTPException(
            status_code=404, detail=str(exc) or ErrorDetail.ATTACHMENT_NOT_FOUND
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/attachments")
async def list_attachments(session_id: str, request: Request) -> list[dict]:
    """列出会话的所有附件。"""
    await _require_session(session_id, request)
    runtime = _get_file_runtime(request)
    return await runtime.list_session_files(session_id)


@router.post("/attachments", status_code=202)
async def upload_attachments(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> list[dict[str, object]]:
    """上传会话附件，并通过知识文档后台队列解析和建立索引。"""
    container = get_runtime_container(request)
    await _require_session(session_id, request)
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个文件")

    file_runtime = container.file_runtime
    created = []
    try:
        for upload in files:
            filename = Path((upload.filename or "upload.bin").replace("\\", "/")).name
            if not filename or "\x00" in filename:
                raise HTTPException(status_code=400, detail=ErrorDetail.INVALID_FILENAME)
            try:
                file_runtime.adapter_registry.select(
                    filename, upload.content_type or ""
                )
            except ValueError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc

            async def chunks():
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

            try:
                stored = await file_runtime.storage.save_stream(chunks())
            except ValueError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc

            attachment = await file_runtime.repository.create_attachment(
                session_id=session_id,
                filename=filename,
                mime_type=upload.content_type or "application/octet-stream",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.storage_key,
            )
            created.append(attachment)
            await container.db.knowledge_document_jobs.enqueue_job(attachment.id)
    except Exception:
        for attachment in created:
            try:
                await container.db.knowledge_document_jobs.cancel_attachment_job(
                    attachment.id
                )
                await file_runtime.repository.soft_delete_attachment(
                    attachment.id, session_id
                )
            except Exception:
                logger.exception(
                    "chat_attachment_upload_compensation_failed",
                    attachment_id=attachment.id,
                )
        try:
            await file_runtime.cleanup_unreferenced_blobs()
        except Exception:
            logger.exception("chat_attachment_upload_blob_cleanup_failed")
        raise
    finally:
        for upload in files:
            await upload.close()

    return [attachment_to_payload(item) for item in created]


@router.get("/attachment-types")
async def get_supported_attachment_types(
    session_id: str, request: Request
) -> dict[str, list[str]]:
    """返回当前文件适配器支持选择的扩展名。"""
    await _require_session(session_id, request)
    runtime = _get_file_runtime(request)
    return {"extensions": runtime.adapter_registry.supported_extensions()}


@router.get("/attachments/{file_id}")
async def get_attachment(session_id: str, file_id: str, request: Request) -> dict:
    """获取附件详情（含元数据）。"""
    await _require_session(session_id, request)
    runtime = _get_file_runtime(request)
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
    await _require_session(session_id, request)
    container = get_runtime_container(request)
    runtime = container.file_runtime
    attachment = await runtime.repository.get_attachment(file_id, session_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.ATTACHMENT_NOT_FOUND)
    await container.db.knowledge_document_jobs.cancel_attachment_job(file_id)
    deleted = await runtime.repository.soft_delete_attachment(file_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=ErrorDetail.ATTACHMENT_NOT_FOUND)
    await runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "file_id": file_id}
