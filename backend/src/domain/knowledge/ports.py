"""知识库及其文档的持久化端口。"""

from __future__ import annotations

from typing import Protocol

from domain.files.entities import Attachment

from .entities import KnowledgeBase


class KnowledgeBaseRepository(Protocol):
    """知识库元数据持久化能力。"""

    async def create(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...
    async def get(self, knowledge_base_id: str) -> KnowledgeBase | None: ...
    async def list_knowledge_bases(self) -> list[KnowledgeBase]: ...
    async def save(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...
    async def delete(self, knowledge_base_id: str) -> bool: ...


class KnowledgeDocumentRepository(Protocol):
    """知识库文档生命周期端口。"""

    async def create(self, attachment: Attachment) -> Attachment: ...
    async def get(
        self, attachment_id: str, *, knowledge_base_id: str
    ) -> Attachment | None: ...
    async def list_documents(self, knowledge_base_id: str) -> list[Attachment]: ...
    async def list_versions(
        self, attachment_id: str, *, knowledge_base_id: str
    ) -> list[Attachment]: ...
    async def latest_for_filename(
        self, filename: str, *, knowledge_base_id: str
    ) -> Attachment | None: ...
    async def soft_delete(self, attachment_id: str, *, knowledge_base_id: str) -> bool: ...


__all__ = ["KnowledgeBaseRepository", "KnowledgeDocumentRepository"]
