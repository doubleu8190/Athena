"""记忆核心的请求、结果与检索端口。"""

from __future__ import annotations

from builtins import list as List
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from domain.memory.lifecycle import LoadPolicy, MemoryKind


@dataclass(frozen=True, slots=True)
class MemorySource:
    """不透明的记忆来源引用。"""

    kind: str
    reference: str
    source_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Memory:
    """一条用户或范围内可用的记忆。"""

    id: str
    namespace_id: str
    scope_key: str
    content: str
    status: str
    origin: str
    version: int
    expires_at: datetime | None
    sources: tuple[MemorySource, ...]
    created_at: datetime
    updated_at: datetime
    kind: MemoryKind = MemoryKind.FACT
    load_policy: LoadPolicy = LoadPolicy.ON_DEMAND
    content_hash: str | None = None
    explicit_long_term: bool = False
    last_evidenced_at: datetime | None = None
    superseded_by_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """后台提取决策可见的相似记忆快照。"""

    id: str
    namespace_id: str
    scope_key: str
    content: str
    version: int
    status: Literal["active", "archived"]
    kind: MemoryKind
    load_policy: LoadPolicy
    expires_at: datetime | None
    updated_at: datetime
    sources: tuple[MemorySource, ...]


@dataclass(frozen=True, slots=True)
class AccessContext:
    """由可信应用服务生成的记忆访问授权快照。"""

    namespace_id: str
    readable_scopes: tuple[str, ...]
    writable_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """带召回评分的记忆。"""

    memory: Memory
    score: float
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallRequest:
    """限制在可信访问快照范围内的召回请求。"""

    access: AccessContext
    scope_keys: tuple[str, ...]
    query: str
    scope_priority: tuple[str, ...] = ()
    limit: int = 8
    token_budget: int = 1200


@dataclass(frozen=True, slots=True)
class RecallResult:
    """混合召回结果。"""

    status: str
    hits: tuple[MemoryHit, ...] = ()
    used_tokens: int = 0
    truncated: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallSnapshot:
    """同一数据库快照中的读取开关与记忆正文、来源。"""

    read_enabled: bool
    memories: dict[str, Memory]


class RerankerPort(Protocol):
    """对查询和记忆正文打相关性分数。"""

    async def score(self, query: str, contents: list[str]) -> list[float]: ...


class MemoryRepositoryPort(Protocol):
    """记忆服务使用的持久化操作。"""

    async def settings(self, namespace_id: str) -> tuple[bool, bool]: ...
    async def search_keyword(
        self, namespace: str, scopes: tuple[str, ...], query: str, limit: int
    ) -> list[tuple[Memory, float]]: ...
    async def search_vector(
        self, namespace: str, scopes: tuple[str, ...], vector: list[float], profile: str, limit: int
    ) -> list[tuple[Memory, float]]: ...
    async def get_many(
        self, ids: tuple[str, ...], namespace: str, scopes: tuple[str, ...]
    ) -> RecallSnapshot: ...
    async def list(
        self, access: AccessContext, scope: str, limit: int, offset: int
    ) -> tuple[Memory, ...]: ...
    async def delete(
        self, access: AccessContext, memory_id: str, expected_version: int
    ) -> bool: ...
    async def edit(
        self,
        access: AccessContext,
        memory_id: str,
        content: str,
        embedding: List[float],
        profile: str,
        expected_version: int,
    ) -> bool: ...
    async def update_settings(
        self, namespace: str, read_enabled: bool, auto_extract_enabled: bool
    ) -> None: ...


__all__ = [
    "AccessContext",
    "Memory",
    "MemoryHit",
    "MemorySource",
    "MemoryRepositoryPort",
    "RecallRequest",
    "RecallResult",
    "RecallSnapshot",
    "RerankerPort",
    "RetrievedMemory",
]
