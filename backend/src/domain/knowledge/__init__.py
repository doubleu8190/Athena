"""知识库领域实体和值端口。"""

from .entities import KnowledgeBase
from .ports import KnowledgeBaseRepository, KnowledgeDocumentRepository

__all__ = ["KnowledgeBase", "KnowledgeBaseRepository", "KnowledgeDocumentRepository"]
