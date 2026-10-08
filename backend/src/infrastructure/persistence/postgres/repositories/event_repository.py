"""PostgreSQL durable event-store implementation."""

from ._runtime import PostgresEventRepository as _RuntimeEventRepository


class PostgresEventRepository(_RuntimeEventRepository):
    """PostgreSQL durable event store and realtime publisher."""

__all__ = ["PostgresEventRepository"]
