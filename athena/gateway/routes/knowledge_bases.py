"""独立知识库与文档导入 REST API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from athena.container import RuntimeContainer, get_runtime_container
from athena.core.files.attachment_serialization import attachment_to_payload
from athena.models.file import Attachment, KnowledgeBase
from athena.utils.logging import get_logger

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
logger = get_logger(__name__)


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
    previous = await runtime.db.files.find_latest_knowledge_document(
        knowledge_base_id, filename
    )
    return await runtime.db.files.create_attachment(
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        mime_type=upload.content_type or "application/octet-stream",
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        storage_key=stored.storage_key,
        logical_document_id=(previous.logical_document_id if previous else None),
    )


@router.get("")
async def list_knowledge_bases(request: Request) -> list[KnowledgeBase]:
    """列出全部独立知识库。"""
    return await get_runtime_container(request).db.knowledge_bases.list_all()


@router.post("", status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest, request: Request
) -> KnowledgeBase:
    """创建一个独立知识库。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    return await get_runtime_container(request).db.knowledge_bases.create(
        name, body.description.strip()
    )


@router.get("/attachment-types")
async def get_knowledge_base_attachment_types(
    request: Request,
) -> dict[str, list[str]]:
    """返回知识库文档导入支持的扩展名。"""
    runtime = get_runtime_container(request)
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
    updated = await get_runtime_container(request).db.knowledge_bases.update(
        knowledge_base_id, name, body.description.strip()
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return updated


@router.delete("/{knowledge_base_id}")
async def delete_knowledge_base(
    knowledge_base_id: str, request: Request
) -> dict[str, str]:
    """删除知识库及其全部文档。"""
    runtime = get_runtime_container(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    documents = await runtime.db.files.list_knowledge_base_attachments(
        knowledge_base_id
    )
    for document in documents:
        await runtime.db.knowledge_document_jobs.cancel_attachment_job(document.id)
        await runtime.file_runtime.delete_knowledge_document(
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
    runtime = get_runtime_container(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    return [
        attachment_to_payload(document, include_metadata=True)
        for document in await runtime.db.files.list_knowledge_base_attachments(
            knowledge_base_id
        )
    ]


@router.post("/{knowledge_base_id}/documents", status_code=202)
async def upload_documents(
    knowledge_base_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> list[dict[str, object]]:
    """上传文档，并在响应后异步完成解析和索引。"""
    runtime = get_runtime_container(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个文件")
    created: list[Attachment] = []
    try:
        for upload in files:
            attachment = await _store_document(runtime, knowledge_base_id, upload)
            created.append(attachment)
            # 逐个入队，保证只要接口返回的附件存在，就一定有对应的持久任务。
            # 任一入队失败时，下面的补偿路径会撤销已经保存的附件和 blob。
            await runtime.db.knowledge_document_jobs.enqueue_job(attachment.id)
    except Exception:
        for attachment in created:
            await runtime.db.knowledge_document_jobs.cancel_attachment_job(
                attachment.id
            )
            try:
                await runtime.file_runtime.delete_knowledge_document(
                    attachment.id, knowledge_base_id
                )
            except Exception:
                # 保留首个入队/保存错误，避免补偿异常掩盖真正的 HTTP 错误。
                logger.exception(
                    "knowledge_document_upload_compensation_failed",
                    attachment_id=attachment.id,
                )
        try:
            await runtime.file_runtime.cleanup_unreferenced_blobs()
        except Exception:
            logger.exception("knowledge_document_upload_blob_cleanup_failed")
        raise
    finally:
        for upload in files:
            await upload.close()
    return [attachment_to_payload(attachment) for attachment in created]


@router.delete("/{knowledge_base_id}/documents/{attachment_id}")
async def delete_document(
    knowledge_base_id: str, attachment_id: str, request: Request
) -> dict[str, str]:
    """删除知识库中的指定文档及其索引。"""
    runtime = get_runtime_container(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    await runtime.db.knowledge_document_jobs.cancel_attachment_job(attachment_id)
    deleted = await runtime.file_runtime.delete_knowledge_document(
        attachment_id, knowledge_base_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    await runtime.file_runtime.cleanup_unreferenced_blobs()
    return {"status": "deleted", "attachment_id": attachment_id}


@router.get("/{knowledge_base_id}/documents/{attachment_id}/versions")
async def list_document_versions(
    knowledge_base_id: str, attachment_id: str, request: Request
) -> list[dict[str, object]]:
    """列出指定逻辑文档的全部上传版本。"""
    runtime = get_runtime_container(request)
    await _require_knowledge_base(runtime, knowledge_base_id)
    versions = await runtime.db.files.list_document_versions(
        attachment_id, knowledge_base_id
    )
    if not versions:
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    return [
        attachment_to_payload(version, include_metadata=True)
        for version in versions
    ]
