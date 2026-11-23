"""认证入口异步固定窗口限流：提供 Redis 分布式实现与本地单进程兜底。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from domain.auth import RateLimitUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_KEY_PREFIX = "athena:rl:"
DEFAULT_REDIS_TIMEOUT_SECONDS = 1.0

# 使用原子 Lua 脚本执行固定窗口计数与首次 TTL 设置：
# 1. 避免 INCR 与 PEXPIRE 分两步执行时进程崩溃导致键永不过期（永久封禁）；
# 2. 若已达到上限则不再继续递增写入，并对异常无 TTL 键执行自愈；
# 3. 返回 1 表示放行，返回 0 表示超限拒绝。
_CONSUME_LUA_SCRIPT = (
    "local limit = tonumber(ARGV[2]) "
    "local current = tonumber(redis.call('GET', KEYS[1]) or '0') "
    "if current >= limit then "
    "  if redis.call('PTTL', KEYS[1]) < 0 then "
    "    redis.call('PEXPIRE', KEYS[1], ARGV[1]) "
    "  end "
    "  return 0 "
    "end "
    "current = redis.call('INCR', KEYS[1]) "
    "if current == 1 or redis.call('PTTL', KEYS[1]) < 0 then "
    "  redis.call('PEXPIRE', KEYS[1], ARGV[1]) "
    "end "
    "return 1"
)


class RateLimiter(Protocol):
    """认证服务所需的异步限流能力。"""

    async def consume(self, key: str) -> bool:
        """尝试消费一次额度并返回是否允许。"""

    async def reset(self, key: str) -> None:
        """清除指定 key 的窗口计数。"""


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int


class FixedWindowRateLimiter:
    """单进程内存固定窗口限流器（供单元测试与非生产无 Redis 本地开发兜底）。"""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """配置窗口大小和可替换单调时钟。"""
        if (
            isinstance(limit, bool)
            or limit < 1
            or isinstance(window_seconds, bool)
            or not math.isfinite(window_seconds)
            or window_seconds <= 0
        ):
            raise ValueError("invalid_rate_limit")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, _Window] = {}
        self._lock = Lock()

    async def consume(self, key: str) -> bool:
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

    async def reset(self, key: str) -> None:
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


class RedisFixedWindowRateLimiter:
    """基于 Redis + 原子 Lua 脚本的分布式固定窗口限流器。

    所有限流键统一带独立命名空间前缀（默认 athena:rl:）；Redis 连接失败、超时或
    传输错误一律按 fail-safe 映射为 RateLimitUnavailableError，防止暴力破解绕过限流。
    """

    def __init__(
        self,
        client: Redis,
        *,
        limit: int,
        window_seconds: float,
        key_prefix: str = DEFAULT_RATE_LIMIT_KEY_PREFIX,
        timeout_seconds: float = DEFAULT_REDIS_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(limit, bool)
            or limit < 1
            or isinstance(window_seconds, bool)
            or not math.isfinite(window_seconds)
            or window_seconds <= 0
        ):
            raise ValueError("invalid_rate_limit")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid_rate_limit_timeout")
        if not key_prefix or not key_prefix.strip():
            raise ValueError("invalid_rate_limit_prefix")
        self._client = client
        self._limit = limit
        self._window_ms = max(1, math.ceil(window_seconds * 1000))
        self._key_prefix = key_prefix
        self._timeout_seconds = timeout_seconds

    def _namespaced_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    async def consume(self, key: str) -> bool:
        """原子扣减一次窗口额度；Redis 异常时 fail-safe 抛出 RateLimitUnavailableError。"""
        namespaced_key = self._namespaced_key(key)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                allowed = await cast(
                    Awaitable[int],
                    self._client.eval(
                        _CONSUME_LUA_SCRIPT,
                        1,
                        namespaced_key,
                        str(self._window_ms),
                        str(self._limit),
                    ),
                )
        except asyncio.CancelledError:
            raise
        except (RedisError, TimeoutError, OSError) as error:
            logger.warning(
                "auth_rate_limit_unavailable operation=consume exception_type=%s",
                type(error).__name__,
            )
            raise RateLimitUnavailableError("rate_limit_unavailable") from error
        return bool(int(allowed))

    async def reset(self, key: str) -> None:
        """删除指定 key 的 Redis 限流计数窗口。"""
        namespaced_key = self._namespaced_key(key)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._client.delete(namespaced_key)
        except asyncio.CancelledError:
            raise
        except (RedisError, TimeoutError, OSError) as error:
            logger.warning(
                "auth_rate_limit_unavailable operation=reset exception_type=%s",
                type(error).__name__,
            )
            raise RateLimitUnavailableError("rate_limit_unavailable") from error


__all__ = [
    "DEFAULT_RATE_LIMIT_KEY_PREFIX",
    "DEFAULT_REDIS_TIMEOUT_SECONDS",
    "FixedWindowRateLimiter",
    "RateLimitUnavailableError",
    "RateLimiter",
    "RedisFixedWindowRateLimiter",
]
