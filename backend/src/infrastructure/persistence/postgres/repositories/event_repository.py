"""PostgreSQL durable event-store implementation."""

from domain.events import (
    EventHistoryPort,
    EventPublisherPort,
    EventSubscriptionPort,
    RealtimeTransportPort,
)

from ._runtime import PostgresEventRepository as _RuntimeEventRepository


class PostgresEventRepository(
    _RuntimeEventRepository,
    EventPublisherPort,
    EventHistoryPort,
    EventSubscriptionPort,
    RealtimeTransportPort,
):
    """PostgreSQL durable event store and realtime publisher."""

__all__ = ["PostgresEventRepository"]
