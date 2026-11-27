"""Target shared identifiers and serialization helpers."""

from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str = "") -> str:
    value = uuid4().hex
    return f"{prefix}_{value}" if prefix else value


__all__ = ["new_id"]
