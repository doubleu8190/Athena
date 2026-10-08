"""PostgreSQL durable event-store implementation."""

from .runtime_repositories import PostgresEventRepository as _RuntimeEventRepository


class PostgresEventRepository(_RuntimeEventRepository):
    """PostgreSQL durable event store and realtime publisher."""

__all__ = ["PostgresEventRepository"]
