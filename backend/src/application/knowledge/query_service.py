"""知识库查询用例。"""

from __future__ import annotations

from domain.files import KnowledgeBase, KnowledgeBaseRepository


class KnowledgeBaseQueryService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self._repository = repository

    async def list(self) -> list[KnowledgeBase]:
        return await self._repository.list_all()

    async def get(self, knowledge_base_id: str) -> KnowledgeBase | None:
        return await self._repository.get(knowledge_base_id)
