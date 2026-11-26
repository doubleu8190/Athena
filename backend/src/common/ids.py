"""Target shared identifiers and serialization helpers."""

from __future__ import annotations

from uuid import uuid4

from common.date import utc_now

# 保留历史导出名 now_utc 兼容，底层统一复用 common.date.utc_now，消除重复实现。
now_utc = utc_now


def new_id(prefix: str = "") -> str:
    value = uuid4().hex
    return f"{prefix}_{value}" if prefix else value


__all__ = ["new_id", "now_utc"]
