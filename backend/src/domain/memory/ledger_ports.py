"""记忆提取快照和任务回顾端口。"""

from __future__ import annotations

from typing import Protocol

from domain.leases import LeaseGrant
from .contracts import (
    MemoryEpisode,
    MemoryFactExtractionSnapshot,
    MemorySourceReference,
)
from .entities import MemoryEpisodeSearchRequest


class MemorySnapshotPort(Protocol):
    """保管轮次的冻结事实提取清单。"""

    async def get_extraction_snapshot(
        self, turn_id: str
    ) -> MemoryFactExtractionSnapshot | None:
        """读取轮次已冻结的提取结果。"""
        ...

    async def save_extraction_snapshot(
        self, snapshot: MemoryFactExtractionSnapshot, *, lease: LeaseGrant
    ) -> MemoryFactExtractionSnapshot:
        """保存或返回并发处理时胜出的冻结结果。"""
        ...



class MemoryEpisodeRepositoryPort(Protocol):
    """任务回顾的幂等持久化能力。"""

    async def get_by_turn(self, turn_id: str) -> MemoryEpisode | None:
        """按来源轮次读取已有回顾；不存在时返回 None。"""
        ...

    async def create_if_absent(
        self, episode: MemoryEpisode, *, lease: LeaseGrant
    ) -> MemoryEpisode | None:
        """首次写入同轮回顾并返回持久化版本。"""
        ...

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryEpisode], int]:
        """列出可管理的任务回顾并返回总数。"""
        ...

    async def forget(self, episode_id: str) -> bool:
        """记录来源抑制并移除回顾。"""
        ...

    async def search(
        self, session_id: str, request: MemoryEpisodeSearchRequest
    ) -> list[MemoryEpisode]:
        """只在给定会话中按词项搜索有界任务回顾。

        参数：session_id 是必含范围；request 可额外允许指定的 project_id。
        返回：最多 request.limit 条当前会话或显式项目空间回顾。
        异常：存储读取错误向调用方传播。
        """
        ...

    async def expand_sources(
        self,
        episode_id: str,
        *,
        session_id: str,
        project_id: str | None,
        limit: int,
    ) -> tuple[MemorySourceReference, ...]:
        """在重新核验记忆范围后展开回顾关联的有限原始来源。"""
        ...


__all__ = [
    "MemoryEpisodeRepositoryPort",
    "MemorySnapshotPort",
]
