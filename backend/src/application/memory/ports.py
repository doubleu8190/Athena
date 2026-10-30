"""抽取、归并和持久化记忆所依赖的应用端口。
"""

from __future__ import annotations

from typing import Protocol
from domain.leases import LeaseGrant

from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryEpisodeProposal,
    MemoryResolution,
)


class MemoryFactExtractor(Protocol):
    """从已完成对话中抽取记忆候选的应用端口。
    """
    async def extract(
        self, turn: CompletedTurn, *, lease: LeaseGrant
    ) -> list[MemoryCandidate]:
        """从已完成对话轮次中抽取记忆候选。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：无额外运行时限制。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。

        返回：
        - list[MemoryCandidate]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class MemoryEpisodeSummarizer(Protocol):
    """将已完成对话整理为记忆回顾提案的应用端口。
    """
    async def summarize(self, turn: CompletedTurn) -> MemoryEpisodeProposal:
        """将已完成对话轮次整理为记忆回顾提案。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：无额外运行时限制。

        返回：
        - MemoryEpisodeProposal：保存对话摘要、来源和有效状态的记忆回顾记录。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class MemoryResolver(Protocol):
    """归并记忆候选并确定持久化动作的应用端口。
    """
    async def resolve(
        self, candidates: list[MemoryCandidate]
    ) -> list[MemoryResolution]:
        """归并记忆候选并生成持久化决议。

        参数：
        - `candidates`（list[MemoryCandidate]）：各检索或计算阶段产生的候选项集合；约束：无额外运行时限制。

        返回：
        - list[MemoryResolution]：符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = [
    "MemoryEpisodeSummarizer",
    "MemoryFactExtractor",
    "MemoryResolver",
]
