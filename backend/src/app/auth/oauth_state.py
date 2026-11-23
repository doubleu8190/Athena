"""OAuth 一次性防重放 state 存储（Redis 原子 SET EX + GETDEL 与非生产内存实现）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

from redis.asyncio import Redis
from redis.exceptions import RedisError

from domain.auth import InvalidReturnToError, OAuthStateUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_OAUTH_STATE_TTL_SECONDS = 300
DEFAULT_OAUTH_STATE_KEY_PREFIX = "athena:oauth:state:"
DEFAULT_OAUTH_REDIS_TIMEOUT_SECONDS = 1.0
MAX_RETURN_TO_LENGTH = 1024
MAX_OAUTH_STATE_LENGTH = 256
_MAX_IN_MEMORY_STATES = 2048


@dataclass(frozen=True, slots=True)
class OAuthStatePayload:
    """一次性 OAuth state 绑定的提供方与校验后的站内回跳路径。"""

    provider: str
    return_to: str


class OAuthStateStore(Protocol):
    """一次性 OAuth state 签发与原子消费端口。"""

    async def issue(self, *, provider: str, return_to: str) -> str:
        """签发高熵随机 state 并以有界 TTL 写入存储。"""

    async def consume(self, state: str, *, provider: str) -> OAuthStatePayload | None:
        """原子一次性消费 state；不存在、过期或提供方不匹配返回 None。"""


def validate_safe_return_to(return_to: str | None) -> str:
    """校验登录后跳转路径，仅允许单斜杠开头的站内相对路径，防止开放重定向与头注入。"""
    if return_to is None or return_to == "":
        return "/"
    if len(return_to) > MAX_RETURN_TO_LENGTH:
        raise InvalidReturnToError("invalid_return_to")
    # 拒绝 ASCII 控制字符（含 CR/LF/NUL）及反斜杠（防止 /\evil.com 被浏览器规范为协议相对 URL）。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in return_to) or "\\" in return_to:
        raise InvalidReturnToError("invalid_return_to")
    if not return_to.startswith("/") or return_to.startswith("//"):
        raise InvalidReturnToError("invalid_return_to")
    parsed = urlsplit(return_to)
    if parsed.scheme or parsed.netloc:
        raise InvalidReturnToError("invalid_return_to")
    return return_to


def _is_valid_state_token(state: str) -> bool:
    """过滤超长、空白或含控制字符的异常 state 参数。"""
    if not state or len(state) > MAX_OAUTH_STATE_LENGTH:
        return False
    return not any(ord(ch) <= 32 or ord(ch) == 127 for ch in state)


@dataclass(slots=True)
class _MemoryStateEntry:
    payload: OAuthStatePayload
    expires_at: float


class InMemoryOAuthStateStore(OAuthStateStore):
    """单进程内存一次性 state 存储，仅供单元测试与非生产无 Redis 本地开发使用。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_OAUTH_STATE_TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
        state_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("invalid_oauth_state_ttl")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._state_factory = state_factory or (lambda: token_urlsafe(32))
        self._entries: dict[str, _MemoryStateEntry] = {}
        self._lock = Lock()

    async def issue(self, *, provider: str, return_to: str) -> str:
        """写入带过期时间的一次性 state。"""
        safe_return_to = validate_safe_return_to(return_to)
        state = self._state_factory()
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            if len(self._entries) >= _MAX_IN_MEMORY_STATES:
                oldest_key = next(iter(self._entries))
                self._entries.pop(oldest_key, None)
            self._entries[state] = _MemoryStateEntry(
                payload=OAuthStatePayload(provider=provider, return_to=safe_return_to),
                expires_at=now + self._ttl_seconds,
            )
        return state

    async def consume(self, state: str, *, provider: str) -> OAuthStatePayload | None:
        """在锁内原子弹出并校验过期时间与提供方，保证每个 state 至多成功消费一次。"""
        if not _is_valid_state_token(state):
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.pop(state, None)
        if entry is None or now >= entry.expires_at or entry.payload.provider != provider:
            return None
        return entry.payload

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, item in self._entries.items() if now >= item.expires_at]
        for key in expired:
            self._entries.pop(key, None)


class RedisOAuthStateStore(OAuthStateStore):
    """基于 Redis 原子 SET EX 与 GETDEL 的分布式一次性 OAuth state 存储。"""

    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = DEFAULT_OAUTH_STATE_TTL_SECONDS,
        key_prefix: str = DEFAULT_OAUTH_STATE_KEY_PREFIX,
        timeout_seconds: float = DEFAULT_OAUTH_REDIS_TIMEOUT_SECONDS,
        state_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("invalid_oauth_state_ttl")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid_oauth_state_timeout")
        if not key_prefix or not key_prefix.strip():
            raise ValueError("invalid_oauth_state_prefix")
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        self._timeout_seconds = timeout_seconds
        self._state_factory = state_factory or (lambda: token_urlsafe(32))

    def _namespaced_key(self, state: str) -> str:
        return f"{self._key_prefix}{state}"

    async def issue(self, *, provider: str, return_to: str) -> str:
        """使用 Redis SET EX 300 原子写入一次性 state 载荷。"""
        safe_return_to = validate_safe_return_to(return_to)
        state = self._state_factory()
        encoded = json.dumps(
            {"provider": provider, "return_to": safe_return_to},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._client.set(
                    self._namespaced_key(state),
                    encoded,
                    ex=self._ttl_seconds,
                )
        except asyncio.CancelledError:
            raise
        except (RedisError, TimeoutError, OSError) as error:
            logger.warning(
                "oauth_state_unavailable operation=issue exception_type=%s",
                type(error).__name__,
            )
            raise OAuthStateUnavailableError("oauth_state_unavailable") from error
        return state

    async def consume(self, state: str, *, provider: str) -> OAuthStatePayload | None:
        """使用 Redis GETDEL 原子读取并删除 state，彻底杜绝并发重放与 CSRF。"""
        if not _is_valid_state_token(state):
            return None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw = await self._client.getdel(self._namespaced_key(state))
        except asyncio.CancelledError:
            raise
        except (RedisError, TimeoutError, OSError) as error:
            logger.warning(
                "oauth_state_unavailable operation=consume exception_type=%s",
                type(error).__name__,
            )
            raise OAuthStateUnavailableError("oauth_state_unavailable") from error
        if raw is None:
            return None
        try:
            raw_text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            parsed = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        stored_provider = parsed.get("provider")
        stored_return_to = parsed.get("return_to")
        if stored_provider != provider or not isinstance(stored_return_to, str):
            return None
        try:
            safe_return_to = validate_safe_return_to(stored_return_to)
        except InvalidReturnToError:
            return None
        return OAuthStatePayload(provider=provider, return_to=safe_return_to)


__all__ = [
    "DEFAULT_OAUTH_REDIS_TIMEOUT_SECONDS",
    "DEFAULT_OAUTH_STATE_KEY_PREFIX",
    "DEFAULT_OAUTH_STATE_TTL_SECONDS",
    "InMemoryOAuthStateStore",
    "OAuthStatePayload",
    "OAuthStateStore",
    "RedisOAuthStateStore",
    "validate_safe_return_to",
]
