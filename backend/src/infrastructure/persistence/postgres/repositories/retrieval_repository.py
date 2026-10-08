"""PostgreSQL retrieval trace query and recording implementation."""

from ._runtime import PostgresRetrievalRepository as _RuntimeRetrievalRepository


class PostgresRetrievalRepository(_RuntimeRetrievalRepository):
    """PostgreSQL retrieval trace query and recording repository."""


__all__ = ["PostgresRetrievalRepository"]
