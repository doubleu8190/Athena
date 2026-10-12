"""PostgreSQL retrieval trace query and recording implementation."""

from domain.retrieval.query_ports import RetrievalQueryPort

from ._runtime import PostgresRetrievalRepository as _RuntimeRetrievalRepository


class PostgresRetrievalRepository(_RuntimeRetrievalRepository, RetrievalQueryPort):
    """PostgreSQL retrieval trace query and recording repository."""


__all__ = ["PostgresRetrievalRepository"]
