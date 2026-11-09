"""知识库领域公共类型。"""

from domain.knowledge.model import (
    MAX_PROJECT_KNOWLEDGE_BASES,
    ChunkLocator,
    Document,
    DocumentState,
    KnowledgeBase,
    OriginalStorageAccessDeniedError,
    OriginalStorageConfigurationError,
    OriginalStorageError,
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
    ProjectKnowledgeLimitReachedError,
    SearchHit,
    SearchResult,
    TextBlock,
)

__all__ = [
    "MAX_PROJECT_KNOWLEDGE_BASES",
    "ChunkLocator",
    "Document",
    "DocumentState",
    "KnowledgeBase",
    "OriginalStorageAccessDeniedError",
    "OriginalStorageConfigurationError",
    "OriginalStorageError",
    "OriginalStorageIntegrityError",
    "OriginalStorageNotFoundError",
    "OriginalStorageUnavailableError",
    "ProjectKnowledgeLimitReachedError",
    "SearchHit",
    "SearchResult",
    "TextBlock",
]
