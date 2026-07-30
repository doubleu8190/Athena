"""In-memory TTL cache — replaces Redis for session data caching.

Provides the same get/set API as the Redis client but uses a plain dict
with per-entry expiration. Thread-safe via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class MemoryCache:
    """Simple async-safe in-memory cache with TTL support.

    Usage::

        cache = MemoryCache()
        await cache.set("key", "value", ex=3600)
        value = await cache.get("key")
        await cache.delete("key")
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        """Get a value by key. Returns None if not found or expired."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expire_at = entry
            if expire_at is not None and time.time() > expire_at:
                del self._store[key]
                return None
            return value

    async def set(
        self, key: str, value: str, ex: int | None = None
    ) -> bool:
        """Set a value with optional TTL in seconds."""
        async with self._lock:
            expire_at = time.time() + ex if ex is not None else None
            self._store[key] = (value, expire_at)
            return True

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if key existed."""
        async with self._lock:
            existed = key in self._store
            self._store.pop(key, None)
            return existed

    async def ping(self) -> bool:
        """Health check — always True for in-memory cache."""
        return True

    async def clear(self) -> None:
        """Clear all entries."""
        async with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ── Singleton ──────────────────────────────────────────────────────

_default_cache: MemoryCache | None = None


def get_cache() -> MemoryCache:
    """Get or create the default in-memory cache singleton."""
    global _default_cache
    if _default_cache is None:
        _default_cache = MemoryCache()
    return _default_cache


async def close_cache() -> None:
    """Close (clear) the default cache. Called during shutdown."""
    global _default_cache
    if _default_cache is not None:
        await _default_cache.clear()
        _default_cache = None
