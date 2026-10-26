"""记忆提取快照和写入回执端口。"""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    MemoryCandidateSnapshot,
    MemoryEpisode,
    MemoryFactExtractionSnapshot,
    MemoryResolutionProposal,
    MemorySourceReference,
    MemoryWriteOperation,
)
from .entities import MemoryEpisodeSearchRequest, MemoryProjectSpace


class MemoryOperationsPort(Protocol):
    """保管冻结提取清单与可恢复的逐候选操作。"""

    async def get_extraction_snapshot(
        self, turn_id: str
    ) -> MemoryFactExtractionSnapshot | None:
        """读取轮次已冻结的提取结果。"""
        ...

    async def save_extraction_snapshot(
        self, snapshot: MemoryFactExtractionSnapshot
    ) -> MemoryFactExtractionSnapshot:
        """保存或返回并发处理时胜出的冻结结果。"""
        ...

    async def begin_operation(
        self, operation_id: str, turn_id: str, candidate: MemoryCandidateSnapshot
    ) -> MemoryWriteOperation:
        """读取或创建单候选写入回执。"""
        ...

    async def record_decision(
        self, operation_id: str, decision: MemoryResolutionProposal
    ) -> MemoryWriteOperation:
        """在写入记忆前持久化已校验的解析决定。"""
        ...

    async def clear_pending_decision(self, operation_id: str) -> None:
        """目标并发变化后清除仍未提交的失效决定。"""
        ...

    async def skip_pending_turn(self, turn_id: str) -> int:
        """按当前设置关闭尚未提交的该轮候选操作。"""
        ...

    async def complete_operation(
        self,
        operation_id: str,
        *,
        status: str,
        decision: MemoryResolutionProposal,
        result_memory_id: str | None,
        projection_status: str,
    ) -> MemoryWriteOperation:
        """完成操作回执并保存写入及向量投影状态。"""
        ...


class MemoryEpisodeRepositoryPort(Protocol):
    """任务回顾的幂等持久化能力。"""

    async def get_by_turn(self, turn_id: str) -> MemoryEpisode | None:
        """按来源轮次读取已有回顾；不存在时返回 None。"""
        ...

    async def create_if_absent(self, episode: MemoryEpisode) -> MemoryEpisode | None:
        """首次写入同轮回顾并返回持久化版本。"""
        ...

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryEpisode], int]:
        """列出可管理的任务回顾并返回总数。"""
        ...

    async def forget(self, episode_id: str) -> bool:
        """记录来源抑制并移除回顾及其派生经验。"""
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


class MemoryProjectSpaceRepositoryPort(Protocol):
    """显式项目记忆空间的目录读写能力。"""

    async def create(self, project: MemoryProjectSpace) -> MemoryProjectSpace: ...

    async def get(self, project_id: str) -> MemoryProjectSpace | None: ...

    async def list(self) -> list[MemoryProjectSpace]: ...

    async def forget(self, project_id: str) -> bool:
        """Remove a project space and suppress its stored and queued history."""
        ...


__all__ = [
    "MemoryEpisodeRepositoryPort",
    "MemoryOperationsPort",
    "MemoryProjectSpaceRepositoryPort",
]
