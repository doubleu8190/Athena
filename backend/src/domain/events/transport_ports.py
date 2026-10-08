"""事件实时传输端口。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .entities import ApplicationEvent


class RealtimeTransportPort(Protocol):
    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None: ...


__all__ = ["RealtimeTransportPort"]
