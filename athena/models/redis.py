"""Redis async client factory.

DB assignment:
  0 — Session context cache
  1 — Celery broker (task queue)
  2 — Celery result backend
"""

from __future__ import annotations

import redis.asyncio as aioredis

_clients: dict[str, aioredis.Redis] = {}


def get_redis_client(url: str) -> aioredis.Redis:
    """Get or create a shared Redis async client for the given URL.

    The same URL always returns the same client instance (connection pooling
    is handled by redis-py internally).
    """
    if url not in _clients:
        _clients[url] = aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _clients[url]


async def close_redis_clients() -> None:
    """Close all Redis client connections (for graceful shutdown)."""
    for client in _clients.values():
        await client.close()
    _clients.clear()
