"""Knowledge 领域实体到 API 响应 DTO 的共享转换。"""

from __future__ import annotations

from dataclasses import asdict

from api.dto.knowledge import (
    KnowledgeBaseResponse,
    KnowledgeDocumentResponse,
    KnowledgeSearchHit,
    KnowledgeVersionResponse,
)
from domain.knowledge.model import (
    ChunkLocator,
    Document,
    DocumentVersion,
    KnowledgeBase,
    SearchHit,
)

__all__ = [
    "knowledge_base_to_response",
    "knowledge_document_to_response",
    "knowledge_search_hit_to_response",
    "knowledge_version_to_response",
]


def knowledge_base_to_response(item: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库实体转换为响应 DTO。"""
    return KnowledgeBaseResponse(
        id=item.id,
        name=item.name,
        description=item.description,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
    )


def knowledge_document_to_response(item: Document) -> KnowledgeDocumentResponse:
    """将文档实体转换为响应 DTO。"""
    return KnowledgeDocumentResponse(
        id=item.id,
        knowledge_base_id=item.knowledge_base_id,
        title=item.title,
        active_version_id=item.active_version_id,
        pending_version_id=item.pending_version_id,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
    )


def knowledge_version_to_response(item: DocumentVersion) -> KnowledgeVersionResponse:
    """将文档版本实体转换为响应 DTO。"""
    return KnowledgeVersionResponse(
        id=item.id,
        document_id=item.document_id,
        state=item.state.value,
        error_code=item.error_code,
        warnings=list(item.warnings),
        chunk_count=item.chunk_count,
        created_at=item.created_at.isoformat(),
        completed_at=item.completed_at.isoformat() if item.completed_at else None,
    )


def _locator_dict(locator: ChunkLocator) -> dict[str, object]:
    return {key: value for key, value in asdict(locator).items() if value is not None}


def knowledge_search_hit_to_response(hit: SearchHit) -> KnowledgeSearchHit:
    """将检索命中切片转换为响应 DTO。"""
    return KnowledgeSearchHit(
        chunk_id=hit.chunk_id,
        chunk_ids=list(hit.chunk_ids or (hit.chunk_id,)),
        document_id=hit.document_id,
        version_id=hit.version_id,
        title=hit.title,
        text=hit.text,
        locators=[_locator_dict(locator) for locator in hit.locators],
        channels=list(hit.channels),
    )
