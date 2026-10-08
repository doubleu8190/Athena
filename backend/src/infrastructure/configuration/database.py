"""Database configuration contract consumed by infrastructure resources."""

from __future__ import annotations

from typing import Protocol


class PostgresSettings(Protocol):
    postgres_url: str
    postgres_pool_size: int
    postgres_max_overflow: int


__all__ = ["PostgresSettings"]
