"""Redis 共享客户端、Run 实时传输、认证分布式限流与 OAuth state 存储构造。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

from adapter.run.redis_transport import RedisRunTransport
from app.auth.oauth_state import (
    InMemoryOAuthStateStore,
    OAuthStateStore,
    RedisOAuthStateStore,
)
from app.auth.rate_limit import RateLimiter, RedisFixedWindowRateLimiter


@dataclass(frozen=True, slots=True)
class AuthRateLimiters:
    """认证服务使用的三组分布式限流器集合（登录账号、登录 IP、改密）。"""

    login_account: RateLimiter
    login_ip: RateLimiter
    change_password: RateLimiter


def is_production_environment() -> bool:
    """判断当前是否处于 ATHENA_ENV=production 生产运行环境。"""
    return os.getenv("ATHENA_ENV", "").strip().lower() == "production"


def build_redis_client() -> Redis | None:
    """按 ATHENA_REDIS_URL 创建共享异步 Redis 客户端；未配置时返回 None。"""
    redis_url = os.getenv("ATHENA_REDIS_URL", "").strip()
    if not redis_url:
        return None
    return cast(
        Redis,
        Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=1),
    )


def validate_auth_rate_limit_configuration() -> None:
    """生产环境禁止回退到单进程内存限流，缺失 ATHENA_REDIS_URL 时立即阻断启动。"""
    if is_production_environment() and not os.getenv("ATHENA_REDIS_URL", "").strip():
        raise RuntimeError("auth_redis_rate_limit_required")


def validate_feishu_state_store_configuration() -> None:
    """生产环境开启飞书 SSO 时必须使用 Redis 一次性 state 存储，防止多实例 CSRF 校验失效。"""
    if is_production_environment() and not os.getenv("ATHENA_REDIS_URL", "").strip():
        raise RuntimeError("feishu_redis_state_store_required")


def build_auth_rate_limiters(client: Redis) -> AuthRateLimiters:
    """基于共享 Redis 客户端装配登录与改密的分布式固定窗口限流器。"""
    return AuthRateLimiters(
        login_account=RedisFixedWindowRateLimiter(
            client,
            limit=5,
            window_seconds=15 * 60,
        ),
        login_ip=RedisFixedWindowRateLimiter(
            client,
            limit=30,
            window_seconds=15 * 60,
        ),
        change_password=RedisFixedWindowRateLimiter(
            client,
            limit=5,
            window_seconds=15 * 60,
        ),
    )


def build_oauth_state_store(client: Redis | None = None) -> OAuthStateStore:
    """优先使用 Redis 原子 GETDEL state 存储，非生产无 Redis 时回退内存实现。"""
    if client is not None:
        return RedisOAuthStateStore(client)
    return InMemoryOAuthStateStore()


def build_run_transport(client: Redis | None = None) -> RedisRunTransport | None:
    """按共享 Redis 客户端或 ATHENA_REDIS_URL 创建 Run 事件传输。

    传入外部共享 client 时，传输层不接管客户端关闭所有权（由调用方统一释放）；
    未传 client 且 Redis 未配置时返回 None，调用方降级为无实时推送。
    """
    if client is not None:
        return RedisRunTransport(client, owns_client=False)
    redis_client = build_redis_client()
    if redis_client is None:
        return None
    return RedisRunTransport(redis_client)


__all__ = [
    "AuthRateLimiters",
    "build_auth_rate_limiters",
    "build_oauth_state_store",
    "build_redis_client",
    "build_run_transport",
    "is_production_environment",
    "validate_auth_rate_limit_configuration",
    "validate_feishu_state_store_configuration",
]
