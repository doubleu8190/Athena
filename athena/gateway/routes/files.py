"""附件 REST API。

提供附件列表、详情和删除接口。文件上传与消息提交统一通过
``POST /sessions/{session_id}/runs`` 的 multipart 请求完成。
所有端点挂载在 ``/sessions/{session_id}`` 前缀下。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from athena.core.files.runtime import FileAccessError
from athena.contracts.errors import ErrorDetail
from athena.container import get_runtime_container

router = APIRouter(prefix="/sessions/{session_id}", tags=["files"])


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
    runtime = _get_file_runtime(request)
    deleted = await runtime.repository.soft_delete_attachment(file_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=ErrorDetail.ATTACHMENT_NOT_FOUND)
    await runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "file_id": file_id}
