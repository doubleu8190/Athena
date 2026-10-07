"""记忆事实提取应用用例。"""

from __future__ import annotations

from domain.memory import (
    CompletedTurn,
    MemoryFactExtractorPort,
    MemoryPort,
    MemoryResolverPort,
    MemoryWriteCommand,
    MemoryWriteTriggerPort,
    ResolutionAction,
)


class MemoryWriteWorkflow:
    """执行触发、提取、解析和持久化，不依赖 LLM 或数据库实现。"""

    def __init__(
        self,
        memory: MemoryPort,
        trigger: MemoryWriteTriggerPort,
        extractor: MemoryFactExtractorPort,
        resolver: MemoryResolverPort,
    ) -> None:
        self._memory = memory
        self._trigger = trigger
        self._extractor = extractor
        self._resolver = resolver

    async def process(self, turn: CompletedTurn) -> int:
        """处理一轮对话并返回实际写入的候选数量。"""
        if not self._trigger.should_process(turn):
            return 0
        candidates = await self._extractor.extract(turn)
        resolutions = await self._resolver.resolve(candidates)
        written = 0
        for resolution in resolutions:
            if resolution.action is ResolutionAction.CREATE:
                await self._memory.add(
                    MemoryWriteCommand(
                        content=resolution.candidate.content,
                        metadata={
                            "session_id": turn.session_id,
                            "source_turn_id": turn.turn_id,
                            "type": resolution.candidate.memory_type,
                            "category": resolution.candidate.category,
                            "confidence": resolution.candidate.confidence,
                            "source": "extraction",
                        },
                        operation_key=f"turn:{turn.turn_id}:{written}",
                    )
                )
                written += 1
            elif resolution.action in {ResolutionAction.UPDATE, ResolutionAction.SUPERSEDE} and resolution.target_memory_id:
                await self._memory.revise(
                    resolution.target_memory_id,
                    resolution.final_content or resolution.candidate.content,
                    metadata={"source_turn_id": turn.turn_id, "session_id": turn.session_id},
                    operation_key=f"turn:{turn.turn_id}:{written}",
                )
                written += 1
        return written


__all__ = ["MemoryWriteWorkflow"]
