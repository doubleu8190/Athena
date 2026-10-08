"""PostgreSQL run and command-store implementation.

The implementation remains in the migration bundle for now; this module is
the stable capability-level import used by composition roots and new callers.
"""

from .runtime_repositories import PostgresRunRepository as _RuntimeRunRepository


class PostgresRunRepository(_RuntimeRunRepository):
    """PostgreSQL command queue and run lifecycle repository."""

__all__ = ["PostgresRunRepository"]
