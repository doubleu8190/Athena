"""知识库领域实体和值端口。"""

from .entities import KnowledgeBase
from .ports import KnowledgeBaseRepository, KnowledgeDocumentRepository

# Legacy imports used by existing callers during the package migration.

__all__ = ["KnowledgeBase", "KnowledgeBaseRepository", "KnowledgeDocumentRepository"]
