"""独立知识库、文档导入以及会话授权 REST API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from athena.container import RuntimeContainer, runtime_from
from athena.core.files.converters import _attachment_to_public
from athena.models.file import Attachment, AttachmentStatus, KnowledgeBase


router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
session_router = APIRouter(prefix="/sessions", tags=["knowledge-bases"])


class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库请求。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class KnowledgeBaseUpdateRequest(BaseModel):
    """更新知识库请求。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


async def _require_knowledge_base(
    runtime: RuntimeContainer, knowledge_base_id: str
) -> KnowledgeBase:
    """读取知识库，不存在时转换为 HTTP 404。

    参数：
        runtime (RuntimeContainer): 当前应用运行时容器。
        knowledge_base_id (str): 知识库唯一标识。

    返回值：
        KnowledgeBase: 已确认存在且未删除的知识库。

    异常：
        HTTPException: 知识库不存在或已删除时返回 404。
    """
    knowledge_base = await runtime.db.knowledge_bases.get(knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return knowledge_base


async def _process_document(runtime: RuntimeContainer, attachment_id: str) -> None:
    """在请求返回后解析并索引一个知识库文档。

    参数：
        runtime (RuntimeContainer): 当前应用运行时容器。
        attachment_id (str): 待处理文档的附件标识。

    返回值：
        None: 最终结果写回附件状态。

    异常：
        处理错误会被转换为 ``failed`` 状态，不向后台任务调度器传播。
    """
    try:
        await runtime.file_runtime.parse_attachment(attachment_id)
        await runtime.file_runtime.index_attachment(attachment_id)
        await runtime.db.files.update_attachment(
            attachment_id,
            status=AttachmentStatus.READY.value,
            error_message=None,
        )
    except Exception as exc:
        await runtime.db.files.update_attachment(
            attachment_id,
            status=AttachmentStatus.FAILED.value,
            error_message=str(exc),
        )


async def _store_document(
    runtime: RuntimeContainer,
    knowledge_base_id: str,
    upload: UploadFile,
) -> Attachment:
    """校验并保存一个知识库文档的原始内容和附件记录。

    参数：
        runtime (RuntimeContainer): 当前应用运行时容器。
        knowledge_base_id (str): 文档所属知识库标识。
        upload (UploadFile): multipart 上传文件。

    返回值：
        Attachment: 状态为 ``uploaded`` 的知识库文档记录。

    异常：
        HTTPException: 文件名、格式或大小不符合要求时返回对应 4xx。
    """
    filename = Path((upload.filename or "upload.bin").replace("\\", "/")).name
    if not filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    try:
        runtime.file_runtime.adapter_registry.select(
            filename, upload.content_type or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    async def chunks():
        """按固定大小读取上传流，避免把大文件一次载入内存。"""
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            yield chunk

    try:
        stored = await runtime.file_runtime.storage.save_stream(chunks())
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return await runtime.db.files.create_attachment(
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        mime_type=upload.content_type or "application/octet-stream",
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        storage_key=stored.storage_key,
    )


@router.get("")
async def list_knowledge_bases(request: Request) -> list[KnowledgeBase]:
    """列出全部独立知识库。"""
    return await runtime_from(request).db.knowledge_bases.list_all()


@router.post("", status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest, request: Request
) -> KnowledgeBase:
    """创建一个独立知识库。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    return await runtime_from(request).db.knowledge_bases.create(
        name, body.description.strip()
    )


@router.get("/attachment-types")
async def get_knowledge_base_attachment_types(
    request: Request,
) -> dict[str, list[str]]:
    """返回知识库文档导入支持的扩展名。"""
    runtime = runtime_from(request)
    return {"extensions": runtime.file_runtime.adapter_registry.supported_extensions()}


@router.patch("/{knowledge_base_id}")
async def update_knowledge_base(
    knowledge_base_id: str,
    body: KnowledgeBaseUpdateRequest,
    request: Request,
) -> KnowledgeBase:
    """更新知识库名称和说明。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    updated = await runtime_from(request).db.knowledge_bases.update(
        knowledge_base_id, name, body.description.strip()
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return updated


@router.delete("/{knowledge_base_id}")
async def delete_knowledge_base(
    knowledge_base_id: str, request: Request
) -> dict[str, str]:
    """删除知识库、全部文档和所有会话绑定。"""
    runtime = runtime_from(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    documents = await runtime.db.files.list_knowledge_base_attachments(
        knowledge_base_id
    )
    for document in documents:
        await runtime.db.files.soft_delete_knowledge_base_attachment(
            document.id, knowledge_base_id
        )
    await runtime.db.knowledge_bases.soft_delete(knowledge_base_id)
    await runtime.file_runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "knowledge_base_id": knowledge_base_id}


@router.get("/{knowledge_base_id}/documents")
async def list_documents(
    knowledge_base_id: str, request: Request
) -> list[dict[str, object]]:
    """列出知识库中的全部文档及处理状态。"""
    runtime = runtime_from(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    return [
        _attachment_to_public(document, include_metadata=True)
        for document in await runtime.db.files.list_knowledge_base_attachments(
            knowledge_base_id
        )
    ]


@router.post("/{knowledge_base_id}/documents", status_code=202)
async def upload_documents(
    knowledge_base_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
) -> list[dict[str, object]]:
    """上传文档，并在响应后异步完成解析和索引。"""
    runtime = runtime_from(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个文件")
    created: list[Attachment] = []
    try:
        for upload in files:
            created.append(
                await _store_document(runtime, knowledge_base_id, upload)
            )
    except Exception:
        for attachment in created:
            await runtime.db.files.soft_delete_knowledge_base_attachment(
                attachment.id, knowledge_base_id
            )
        await runtime.file_runtime.cleanup_unreferenced_blobs()
        raise
    finally:
        for upload in files:
            await upload.close()
    for attachment in created:
        background_tasks.add_task(_process_document, runtime, attachment.id)
    return [_attachment_to_public(attachment) for attachment in created]


@router.delete("/{knowledge_base_id}/documents/{attachment_id}")
async def delete_document(
    knowledge_base_id: str, attachment_id: str, request: Request
) -> dict[str, str]:
    """删除知识库中的指定文档及其索引。"""
    runtime = runtime_from(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    deleted = await runtime.db.files.soft_delete_knowledge_base_attachment(
        attachment_id, knowledge_base_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    await runtime.file_runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "attachment_id": attachment_id}


@session_router.get("/{session_id}/knowledge-bases")
async def list_session_knowledge_bases(
    session_id: str, request: Request
) -> list[KnowledgeBase]:
    """列出会话当前可以访问的知识库。"""
    runtime = runtime_from(request)
    if await runtime.db.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return await runtime.db.knowledge_bases.list_for_session(session_id)


@session_router.put("/{session_id}/knowledge-bases/{knowledge_base_id}")
async def bind_session_knowledge_base(
    session_id: str, knowledge_base_id: str, request: Request
) -> dict[str, str]:
    """授予会话访问指定知识库的权限。"""
    runtime = runtime_from(request)
    if await runtime.db.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await _require_knowledge_base(runtime, knowledge_base_id)
    await runtime.db.knowledge_bases.bind_session(session_id, knowledge_base_id)
    return {"status": "bound", "knowledge_base_id": knowledge_base_id}


@session_router.delete("/{session_id}/knowledge-bases/{knowledge_base_id}")
async def unbind_session_knowledge_base(
    session_id: str, knowledge_base_id: str, request: Request
) -> dict[str, str]:
    """移除会话对指定知识库的访问权限。"""
    runtime = runtime_from(request)
    if await runtime.db.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await runtime.db.knowledge_bases.unbind_session(session_id, knowledge_base_id)
    return {"status": "unbound", "knowledge_base_id": knowledge_base_id}
