"""事件订阅端口。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .entities import ApplicationEvent


class EventSubscriptionPort(Protocol):
    async def open_subscription(self, session_id: str) -> tuple[asyncio.Queue[ApplicationEvent], int]: ...


__all__ = ["EventSubscriptionPort"]
