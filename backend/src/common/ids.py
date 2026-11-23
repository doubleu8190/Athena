"""Target shared identifiers and serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def new_id(prefix: str = "") -> str:
    value = uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = ["new_id", "now_utc"]
