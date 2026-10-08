"""PostgreSQL long-term memory implementation."""

from ._runtime import PostgresMemoryRepository as _RuntimeMemoryRepository


class PostgresMemoryRepository(_RuntimeMemoryRepository):
    """PostgreSQL long-term memory repository."""

__all__ = ["PostgresMemoryRepository"]
