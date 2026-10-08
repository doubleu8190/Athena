"""PostgreSQL run and command-store implementation."""

from ._runtime import PostgresRunRepository as _RuntimeRunRepository


class PostgresRunRepository(_RuntimeRunRepository):
    """PostgreSQL command queue and run lifecycle repository."""

__all__ = ["PostgresRunRepository"]
