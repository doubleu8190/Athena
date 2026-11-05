"""认证入口的进程内固定窗口限流。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol


class RateLimiter(Protocol):
    """认证服务所需的限流能力。"""

    def consume(self, key: str) -> bool:
        """尝试消费一次额度并返回是否允许。"""

    def reset(self, key: str) -> None:
        """清除指定 key 的窗口计数。"""


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int


class FixedWindowRateLimiter:
    """单进程固定窗口限流器。

    该实现只保护单进程部署；横向扩容时必须替换为 Redis 或网关级共享限流。
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """配置窗口大小和可替换单调时钟。"""
        if limit < 1 or window_seconds <= 0:
            raise ValueError("invalid_rate_limit")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, _Window] = {}
        self._lock = Lock()

    def consume(self, key: str) -> bool:
        """在当前窗口消费一次额度，满额时返回 False。"""
        now = self._clock()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now >= window.started_at + self._window_seconds:
                self._windows[key] = _Window(started_at=now, count=1)
                self._purge_expired(now)
                return True
            if window.count >= self._limit:
                return False
            window.count += 1
            return True

    def reset(self, key: str) -> None:
        """清除某个入口的窗口。"""
        with self._lock:
            self._windows.pop(key, None)

    def _purge_expired(self, now: float) -> None:
        """在窗口数量增长时机会式清理过期键。"""
        if len(self._windows) < 1024:
            return
        expired = [
            key
            for key, window in self._windows.items()
            if now >= window.started_at + self._window_seconds
        ]
        for key in expired:
            self._windows.pop(key, None)


__all__ = ["FixedWindowRateLimiter", "RateLimiter"]
