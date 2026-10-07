"""记忆事实提取和后台任务应用用例。"""

from __future__ import annotations

import asyncio

from domain.memory import (
    CompletedTurn,
    MemoryFactExtractorPort,
    MemoryJobPort,
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


class MemoryWorker:
    """可停止、可恢复的记忆后台任务消费者。"""

    def __init__(self, jobs: MemoryJobPort, workflow: MemoryWriteWorkflow, *, poll_interval: float = 1.0) -> None:
        self._jobs = jobs
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """持续领取任务，失败时交给队列决定是否重试。"""
        await self._jobs.recover()
        while not self._stop.is_set():
            job = await self._jobs.claim()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    continue
                continue
            turn = CompletedTurn(
                turn_id=str(job["turn_id"]),
                session_id=str(job["session_id"]),
                user_text=str(job.get("user_text", "")),
                assistant_text=str(job.get("assistant_text", "")),
            )
            try:
                await self._workflow.process(turn)
            except Exception as exc:
                await self._jobs.fail(turn.turn_id, str(exc), retry=True)
            else:
                await self._jobs.succeed(turn.turn_id)

    def stop(self) -> None:
        """请求 worker 在当前任务完成后停止。"""
        self._stop.set()


__all__ = ["MemoryWorker", "MemoryWriteWorkflow"]
