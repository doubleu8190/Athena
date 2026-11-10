"""Redis Run 实时传输构造。"""

from __future__ import annotations

import os

from redis.asyncio import Redis

from adapter.run.redis_transport import RedisRunTransport


def build_run_transport() -> RedisRunTransport | None:
    """按 ATHENA_REDIS_URL 创建 Run 事件传输。

    Redis 未配置时返回 None，调用方降级为无实时推送：Run 的持久化与状态
    流转仍正常进行，只失去跨进程的实时事件分发。
    """
    redis_url = os.getenv("ATHENA_REDIS_URL", "").strip()
    if not redis_url:
        return None
    return RedisRunTransport(
        Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=1)
    )


__all__ = ["build_run_transport"]
