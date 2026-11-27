"""知识库目录、文本导入和搜索 HTTP 接口。"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from api.converter.knowledge import (
    knowledge_base_to_response,
    knowledge_document_to_response,
    knowledge_search_hit_to_response,
    knowledge_version_to_response,
)
from api.dto.knowledge import (
    KnowledgeBaseListResponse,
    KnowledgeBasePatchRequest,
    KnowledgeBaseRequest,
    KnowledgeBaseResponse,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentPatchRequest,
    KnowledgeDocumentResponse,
    KnowledgeImportResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeTextRequest,
    KnowledgeVersionResponse,
)
from api.errors import DomainErrorRoute, domain_error_to_http
from api.security import CurrentAuth
from app.knowledge.service import KnowledgeInputError, KnowledgeService
from common.unset import UNSET, UnsetType
from domain.access import VisibilityGrantInput, VisibilityScope
from domain.knowledge.model import DocumentState
from domain.knowledge.ports import DocumentFileValidator


class _KnowledgeErrorRoute(DomainErrorRoute):
    """在统一领域异常映射之上，兼容知识库仓储/解析器直接抛出的裸 LookupError 与 ValueError。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def knowledge_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as error:
                # 复用统一的 KnowledgeInputError 状态码分流（409/415/413/429/422）。
                mapped = domain_error_to_http(KnowledgeInputError(str(error)))
                assert mapped is not None
                raise mapped from error

        return knowledge_handler


def build_knowledge_router(
    service: KnowledgeService, validator: DocumentFileValidator
) -> APIRouter:
    """将知识库用例绑定到版本化 API。"""
    router = APIRouter(
        prefix="/api/v1/knowledge-bases",
        tags=["knowledge"],
        route_class=_KnowledgeErrorRoute,
    )

    @router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
    async def create_base(
        request: KnowledgeBaseRequest, auth: CurrentAuth
    ) -> KnowledgeBaseResponse:
        created = await service.create_base(
            auth.user.id,
            request.name,
            request.description,
            visibility_scope=VisibilityScope(request.visibility_scope),
            owner_department_id=request.owner_department_id,
            visibility_grants=(
                None
                if request.visibility_grants is None
                else tuple(item.to_domain() for item in request.visibility_grants)
            ),
        )
        return knowledge_base_to_response(created)

    @router.get("", response_model=KnowledgeBaseListResponse)
    async def list_bases(
        auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        visibility_scope: Annotated[
            Literal["company", "department", "personal"] | None, Query()
        ] = None,
        owner_department_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    ) -> KnowledgeBaseListResponse:
        items = await service.list_bases(
            auth.user.id,
            page=page,
            page_size=page_size,
            visibility_scope=(
                None if visibility_scope is None else VisibilityScope(visibility_scope)
            ),
            owner_department_id=owner_department_id,
        )
        return KnowledgeBaseListResponse(
            items=[knowledge_base_to_response(item) for item in items],
            page=page,
            page_size=page_size,
        )

    @router.get("/{base_id}", response_model=KnowledgeBaseResponse)
    async def get_base(base_id: str, auth: CurrentAuth) -> KnowledgeBaseResponse:
        return knowledge_base_to_response(await service.get_base(auth.user.id, base_id))

    @router.patch("/{base_id}", response_model=KnowledgeBaseResponse)
    async def patch_base(
        base_id: str, request: KnowledgeBasePatchRequest, auth: CurrentAuth
    ) -> KnowledgeBaseResponse:
        fields = request.model_fields_set
        if "visibility_scope" in fields and request.visibility_scope is None:
            raise KnowledgeInputError("visibility_scope_invalid")
        visibility_scope_arg: VisibilityScope | UnsetType = (
            VisibilityScope(request.visibility_scope)
            if "visibility_scope" in fields and request.visibility_scope is not None
            else UNSET
        )
        owner_department_arg: str | None | UnsetType = (
            request.owner_department_id if "owner_department_id" in fields else UNSET
        )
        visibility_grants_arg: tuple[VisibilityGrantInput, ...] | None | UnsetType = (
            (
                None
                if request.visibility_grants is None
                else tuple(item.to_domain() for item in request.visibility_grants)
            )
            if "visibility_grants" in fields
            else UNSET
        )
        updated = await service.rename_base(
            auth.user.id,
            base_id,
            request.name,
            request.description,
            visibility_scope=visibility_scope_arg,
            owner_department_id=owner_department_arg,
            visibility_grants=visibility_grants_arg,
        )
        return knowledge_base_to_response(updated)

    @router.delete(
        "/{base_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_base(base_id: str, auth: CurrentAuth) -> Response:
        await service.delete_base(auth.user.id, base_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/{base_id}/documents/text",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_text(
        base_id: str,
        request: KnowledgeTextRequest,
        auth: CurrentAuth,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        document, version_id = await service.create_text_document(
            auth.user.id,
            base_id,
            request.title,
            request.text,
            idempotency_key,
        )
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state=DocumentState.QUEUED,
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/upload",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_document(
        base_id: str,
        auth: CurrentAuth,
        title: Annotated[str, Form(min_length=1, max_length=255)],
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        from app.knowledge.service import MAX_FILE_BYTES

        content = await _stage_upload(file, file.filename or "", MAX_FILE_BYTES, validator)
        document, version_id = await service.create_file_document(
            auth.user.id,
            base_id,
            title,
            file.filename or "",
            bytes(content),
            idempotency_key,
        )
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state=DocumentState.QUEUED,
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/{document_id}/versions/upload",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replace_upload(
        base_id: str,
        document_id: str,
        auth: CurrentAuth,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        from app.knowledge.service import MAX_FILE_BYTES

        content = await _stage_upload(file, file.filename or "", MAX_FILE_BYTES, validator)
        document, version_id = await service.replace_file_document(
            auth.user.id,
            base_id,
            document_id,
            file.filename or "",
            bytes(content),
            idempotency_key,
        )
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state=DocumentState.QUEUED,
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/{document_id}/versions/text",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replace_text(
        base_id: str,
        document_id: str,
        request: KnowledgeTextRequest,
        auth: CurrentAuth,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        document, version_id = await service.replace_text_document(
            auth.user.id,
            base_id,
            document_id,
            request.text,
            idempotency_key,
        )
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state=DocumentState.QUEUED,
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.get("/{base_id}/documents", response_model=KnowledgeDocumentListResponse)
    async def list_documents(
        base_id: str,
        auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> KnowledgeDocumentListResponse:
        items = await service.list_documents(auth.user.id, base_id, page=page, page_size=page_size)
        return KnowledgeDocumentListResponse(
            items=[knowledge_document_to_response(item) for item in items],
            page=page,
            page_size=page_size,
        )

    @router.get("/{base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse)
    async def get_document(
        base_id: str, document_id: str, auth: CurrentAuth
    ) -> KnowledgeDocumentResponse:
        return knowledge_document_to_response(
            await service.get_document(auth.user.id, base_id, document_id)
        )

    @router.get(
        "/{base_id}/documents/{document_id}/versions",
        response_model=list[KnowledgeVersionResponse],
    )
    async def list_versions(
        base_id: str, document_id: str, auth: CurrentAuth
    ) -> list[KnowledgeVersionResponse]:
        versions = await service.list_versions(auth.user.id, base_id, document_id)
        return [knowledge_version_to_response(item) for item in versions]

    @router.post(
        "/{base_id}/documents/{document_id}/versions/{version_id}/retry",
        response_model=KnowledgeVersionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_version(
        base_id: str, document_id: str, version_id: str, auth: CurrentAuth
    ) -> KnowledgeVersionResponse:
        item = await service.retry_version(auth.user.id, base_id, document_id, version_id)
        return knowledge_version_to_response(item)

    @router.patch("/{base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse)
    async def patch_document(
        base_id: str, document_id: str, request: KnowledgeDocumentPatchRequest, auth: CurrentAuth
    ) -> KnowledgeDocumentResponse:
        document = await service.rename_document(
            auth.user.id,
            base_id,
            document_id,
            request.title,
        )
        return knowledge_document_to_response(document)

    @router.delete(
        "/{base_id}/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_document(base_id: str, document_id: str, auth: CurrentAuth) -> Response:
        await service.delete_document(auth.user.id, base_id, document_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/{base_id}/search", response_model=KnowledgeSearchResponse)
    async def search(
        base_id: str, request: KnowledgeSearchRequest, auth: CurrentAuth
    ) -> KnowledgeSearchResponse:
        result = await service.search(auth.user.id, [base_id], request.query, limit=request.limit)
        return KnowledgeSearchResponse(
            hits=[knowledge_search_hit_to_response(hit) for hit in result.hits],
            channels=list(result.channels),
            degraded_reasons=list(result.degraded_reasons),
        )

    @router.get(
        "/{base_id}/documents/{document_id}/versions/{version_id}/source",
        response_class=Response,
        responses={
            200: {
                "description": "Original knowledge document binary content",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"},
                    }
                },
            }
        },
    )
    async def download_source(
        base_id: str, document_id: str, version_id: str, auth: CurrentAuth
    ) -> Response:
        filename, media_type, content = await service.source(
            auth.user.id,
            base_id,
            document_id,
            version_id,
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/{base_id}/chunks/{chunk_id}", response_model=KnowledgeSearchHit)
    async def get_chunk(base_id: str, chunk_id: str, auth: CurrentAuth) -> KnowledgeSearchHit:
        hit = await service.chunk(auth.user.id, base_id, chunk_id)
        return knowledge_search_hit_to_response(hit)

    return router


async def _stage_upload(
    file: UploadFile,
    filename: str,
    maximum: int,
    validator: DocumentFileValidator,
) -> bytes:
    """限制上传并先校验暂存文件，再读取受限大小内容供原件存储。"""
    size = 0
    with tempfile.NamedTemporaryFile(mode="w+b", suffix=".upload", delete=True) as staged:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HTTPException(status_code=413, detail="document_file_too_large")
            await asyncio.to_thread(staged.write, chunk)
        staged.flush()
        # 校验器由组合根注入，API 层不直接依赖 infrastructure 实现。
        await asyncio.to_thread(
            validator.validate_file,
            filename,
            Path(staged.name),
            max_bytes=maximum,
        )
        staged.seek(0)
        return await asyncio.to_thread(staged.read)
