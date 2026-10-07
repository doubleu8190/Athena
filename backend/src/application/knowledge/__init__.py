"""知识库查询用例。"""

from .query_service import KnowledgeBaseQueryService
from .service import KnowledgeBaseNotFoundError, KnowledgeBaseService, KnowledgeDocumentNotFoundError

__all__ = [
    "KnowledgeBaseNotFoundError",
    "KnowledgeBaseQueryService",
    "KnowledgeBaseService",
    "KnowledgeDocumentNotFoundError",
]
