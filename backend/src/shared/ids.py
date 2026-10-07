"""Target shared identifiers and serialization helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def new_id(prefix: str = "") -> str:
    value = uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def monotonic_timestamp_id_factory() -> Callable[[], str]:
    """Return a UTC millisecond ID factory that is monotonic per process."""
    last_timestamp: datetime | None = None

    def create_id() -> str:
        """生成当前进程内单调递增的 UTC 毫秒时间 ID。"""
        nonlocal last_timestamp
        now = datetime.now(timezone.utc)
        current = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        if last_timestamp is not None and current <= last_timestamp:
            current = last_timestamp + timedelta(milliseconds=1)
        last_timestamp = current
        return current.strftime("%Y.%m.%d.%H.%M.%S.%f")[:-3]

    return create_id


__all__ = ["monotonic_timestamp_id_factory", "new_id", "now_utc"]
