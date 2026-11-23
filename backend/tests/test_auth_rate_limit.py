"""认证异步固定窗口限流（单进程与 Redis 分布式）单元测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth.rate_limit import (
    FixedWindowRateLimiter,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


class _FakeRedis:
    """模拟 Redis 固定窗口 Lua 脚本与 TTL 过期的轻量替身。"""

    def __init__(self, *, now: list[float] | None = None) -> None:
        self.now = now if now is not None else [1000.0]
        self.values: dict[str, tuple[int, float | None]] = {}
        self.eval_calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.delete_calls: list[str] = []
        self.error: Exception | None = None

    def _get_live(self, key: str) -> tuple[int, float | None] | None:
        entry = self.values.get(key)
        if entry is None:
            return None
        count, expires_at = entry
        if expires_at is not None and self.now[0] >= expires_at:
            self.values.pop(key, None)
            return None
        return count, expires_at

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        self.eval_calls.append((script, numkeys, keys_and_args))
        if self.error is not None:
            raise self.error
        assert numkeys == 1
        key, window_ms_raw, limit_raw = keys_and_args
        window_seconds = int(window_ms_raw) / 1000.0
        limit = int(limit_raw)
        entry = self._get_live(key)
        current = 0 if entry is None else entry[0]
        expires_at = None if entry is None else entry[1]
        if current >= limit:
            if expires_at is None:
                self.values[key] = (current, self.now[0] + window_seconds)
            return 0
        next_count = current + 1
        if next_count == 1 or expires_at is None:
            expires_at = self.now[0] + window_seconds
        self.values[key] = (next_count, expires_at)
        return 1

    async def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        if self.error is not None:
            raise self.error
        return 1 if self.values.pop(key, None) is not None else 0


def test_fixed_window_limits_and_resets() -> None:
    now = [100.0]
    limiter = FixedWindowRateLimiter(
        limit=2,
        window_seconds=10,
        clock=lambda: now[0],
    )

    async def scenario() -> None:
        assert await limiter.consume("login") is True
        assert await limiter.consume("login") is True
        assert await limiter.consume("login") is False

        now[0] += 10
        assert await limiter.consume("login") is True

        await limiter.reset("login")
        assert await limiter.consume("login") is True

    asyncio.run(scenario())


def test_fixed_window_isolated_by_key() -> None:
    limiter = FixedWindowRateLimiter(
        limit=1,
        window_seconds=10,
        clock=lambda: 1.0,
    )

    async def scenario() -> None:
        assert await limiter.consume("first") is True
        assert await limiter.consume("first") is False
        assert await limiter.consume("second") is True

    asyncio.run(scenario())


def test_redis_fixed_window_counts_blocks_expires_and_resets_with_namespace() -> None:
    now = [1000.0]
    fake_redis = _FakeRedis(now=now)
    limiter = RedisFixedWindowRateLimiter(
        cast(Redis, fake_redis),
        limit=2,
        window_seconds=10.0,
    )

    async def scenario() -> None:
        assert await limiter.consume("login:ip:127.0.0.1") is True
        assert await limiter.consume("login:ip:127.0.0.1") is True
        assert await limiter.consume("login:ip:127.0.0.1") is False
        # 不同 key 在同一命名空间下互不干扰。
        assert await limiter.consume("login:ip:10.0.0.2") is True

        # 验证写入键均携带 athena:rl: 前缀并通过原子 Lua 脚本设置 PEXPIRE。
        assert "athena:rl:login:ip:127.0.0.1" in fake_redis.values
        assert fake_redis.values["athena:rl:login:ip:127.0.0.1"][1] == pytest.approx(1010.0)
        assert "PEXPIRE" in fake_redis.eval_calls[0][0]
        assert fake_redis.eval_calls[0][2] == ("athena:rl:login:ip:127.0.0.1", "10000", "2")

        # reset 删除带命名空间的键，立即恢复额度。
        await limiter.reset("login:ip:127.0.0.1")
        assert fake_redis.delete_calls == ["athena:rl:login:ip:127.0.0.1"]
        assert await limiter.consume("login:ip:127.0.0.1") is True

        # 窗口过期后重新计数。
        assert await limiter.consume("login:ip:127.0.0.1") is True
        assert await limiter.consume("login:ip:127.0.0.1") is False
        now[0] += 10.0
        assert await limiter.consume("login:ip:127.0.0.1") is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "redis_error",
    (
        RedisConnectionError("redis connection refused"),
        TimeoutError("redis timeout"),
        OSError("socket reset"),
    ),
)
def test_redis_fixed_window_maps_backend_failures_to_rate_limit_unavailable(
    redis_error: Exception,
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.error = redis_error
    limiter = RedisFixedWindowRateLimiter(
        cast(Redis, fake_redis),
        limit=5,
        window_seconds=60.0,
    )

    async def scenario() -> None:
        with pytest.raises(RateLimitUnavailableError, match="rate_limit_unavailable"):
            await limiter.consume("login:ip:127.0.0.1")
        with pytest.raises(RateLimitUnavailableError, match="rate_limit_unavailable"):
            await limiter.reset("login:ip:127.0.0.1")

    asyncio.run(scenario())


def test_redis_fixed_window_validates_arguments() -> None:
    fake_redis = cast(Any, _FakeRedis())
    with pytest.raises(ValueError, match="invalid_rate_limit"):
        RedisFixedWindowRateLimiter(fake_redis, limit=0, window_seconds=10)
    with pytest.raises(ValueError, match="invalid_rate_limit"):
        RedisFixedWindowRateLimiter(fake_redis, limit=5, window_seconds=0)
    with pytest.raises(ValueError, match="invalid_rate_limit_timeout"):
        RedisFixedWindowRateLimiter(fake_redis, limit=5, window_seconds=10, timeout_seconds=-1)
    with pytest.raises(ValueError, match="invalid_rate_limit_prefix"):
        RedisFixedWindowRateLimiter(fake_redis, limit=5, window_seconds=10, key_prefix="   ")
