"""知识库查询用例。"""

from __future__ import annotations

from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository
from application.common.responses import KnowledgeBaseResponse


class KnowledgeBaseQueryService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self._repository = repository

    async def list_knowledge_bases(self) -> list[KnowledgeBaseResponse]:
        return [_to_response(value) for value in await self._repository.list_knowledge_bases()]

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseResponse | None:
        value = await self._repository.get(knowledge_base_id)
        return None if value is None else _to_response(value)


def _to_response(value: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库实体转换为应用响应。"""
    return KnowledgeBaseResponse(value.id, value.name, value.description, value.document_count, value.ready_document_count, value.total_size_bytes, value.created_at, value.updated_at)
