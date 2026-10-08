"""PostgreSQL retrieval trace query and recording implementation."""

from .runtime_repositories import PostgresRetrievalQueryRepository as _RuntimeRetrievalRepository


class PostgresRetrievalRepository(_RuntimeRetrievalRepository):
    """PostgreSQL retrieval trace query and recording repository."""


PostgresRetrievalQueryRepository = PostgresRetrievalRepository

__all__ = ["PostgresRetrievalRepository", "PostgresRetrievalQueryRepository"]
