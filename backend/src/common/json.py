"""Shared JSON serialization and UTF-8 bounding helpers."""

from __future__ import annotations

import json
from typing import Any


def dumps(value: Any, *, sort_keys: bool = False) -> str:
    """Serialize a JSON value deterministically without NaN/Infinity."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
    )


def utf8_size(value: Any) -> int:
    """Return the UTF-8 size of a canonical JSON value."""
    return len(dumps(value).encode("utf-8"))


def truncate_utf8(text: str, maximum_bytes: int) -> str:
    """Truncate text without splitting a UTF-8 code point."""
    if maximum_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


__all__ = ["dumps", "truncate_utf8", "utf8_size"]
