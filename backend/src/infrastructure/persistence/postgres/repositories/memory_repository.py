"""PostgreSQL long-term memory implementation."""

from domain.memory import MemoryRepositoryPort

from ._runtime import PostgresMemoryRepository as _RuntimeMemoryRepository


class PostgresMemoryRepository(_RuntimeMemoryRepository, MemoryRepositoryPort):
    """PostgreSQL long-term memory repository."""

__all__ = ["PostgresMemoryRepository"]
