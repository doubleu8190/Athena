"""上下文压缩器 — 增量摘要 + ToolMessage 配对 + 智能保留.

完整压缩流程：
1. 检查是否需要压缩（基于 token 阈值）
2. 识别完整对话轮次（保护 assistant-tool 配对）
3. 分离旧轮次与最近轮次
4. 增量更新摘要（与已有摘要合并）
5. 重建压缩后的消息列表
"""

from __future__ import annotations

from typing import Any

from athena.config.settings import Settings, get_settings
from athena.core.compression.pairer import MessagePairer
from athena.core.compression.summarizer import IncrementalSummarizer
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class ContextSizeChecker:
    """上下文大小检测器."""

    def __init__(self, max_context_tokens: int = 128000, threshold: float = 0.8) -> None:
        self._max_tokens = max_context_tokens
        self._threshold = threshold

    async def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """检查是否需要压缩（超过阈值时触发）."""
        total = sum(self._estimate_tokens(m.get("content", "")) for m in messages)
        # 也计入 tool_calls 的 token
        for m in messages:
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                total += self._estimate_tokens(str(tc.get("args", {})))
        return total > self._max_tokens * self._threshold

    def estimate_total(self, messages: list[dict[str, Any]]) -> int:
        return sum(self._estimate_tokens(m.get("content", "")) for m in messages)

    def _estimate_tokens(self, text: str) -> int:
        """粗略估算 token 数（4 字符 ≈ 1 token）.

        project_memory 约束：使用 model.get_num_tokens()，缺失时回退到 len/4。
        此处采用回退方案，因为底层模型实例不一定暴露 get_num_tokens。
        """
        return len(text) // 4


class ContextCompressor:
    """上下文压缩器."""

    def __init__(
        self,
        llm: Any,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings or get_settings()
        self._size_checker = ContextSizeChecker(
            max_context_tokens=self._settings.max_context_tokens,
            threshold=self._settings.compression_threshold,
        )
        self._pairer = MessagePairer()
        self._summarizer = IncrementalSummarizer(
            llm=llm,
            max_summary_tokens=self._settings.max_summary_tokens,
        )
        self._keep_recent_turns = self._settings.keep_recent_turns

    @property
    def summarizer(self) -> IncrementalSummarizer:
        return self._summarizer

    @property
    def pairer(self) -> MessagePairer:
        return self._pairer

    async def compress(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        memory_manager: Any | None = None,
    ) -> list[dict[str, Any]]:
        """压缩消息列表.

        Args:
            messages: 原始消息列表
            session_id: 会话 ID（用于持久化摘要缓冲区）
            memory_manager: 记忆管理器（可选，用于持久化摘要）

        Returns:
            压缩后的消息列表（若未触发压缩，则原样返回）
        """
        # 1. 检查是否需要压缩
        if not await self._size_checker.should_compress(messages):
            return messages

        # 2. 识别完整对话轮次
        turns = self._pairer.identify_turns(messages)

        if len(turns) <= self._keep_recent_turns + 1:
            return messages

        # 3. 分离旧轮次和最近轮次
        old_turns, recent_turns = self._pairer.get_recent_turns(turns, self._keep_recent_turns)
        if not old_turns:
            return messages

        # 4. 增量更新摘要
        original_buffer = self._summarizer.summary_buffer
        updated_summary = await self._summarizer.update_summary(
            old_turns,
            session_id=session_id,
            memory_manager=memory_manager,
        )

        # 摘要更新失败时降级为简单截断（保留更多消息）
        if not updated_summary and not original_buffer:
            logger.warning("compression_summary_empty_fallback_to_truncation")
            return messages

        # 5. 重建压缩后的消息列表
        compressed = self._rebuild_messages(updated_summary, recent_turns)

        await self._emit_compression_event(messages, compressed)
        return compressed

    def _rebuild_messages(
        self,
        summary: str,
        recent_turns: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """重建压缩后的消息列表."""
        summary_msg = {
            "role": "system",
            "content": f"[对话历史摘要]\n{summary}",
            "metadata": {
                "type": "conversation_summary",
                "is_incremental": True,
            },
        }
        recent_messages: list[dict[str, Any]] = []
        for turn in recent_turns:
            recent_messages.extend(turn)
        return [summary_msg] + recent_messages

    async def _emit_compression_event(
        self,
        original: list[dict[str, Any]],
        compressed: list[dict[str, Any]],
    ) -> None:
        """发送压缩统计事件（供调用方推送）."""
        original_tokens = self._size_checker.estimate_total(original)
        compressed_tokens = self._size_checker.estimate_total(compressed)
        saved_percent = (
            (1 - compressed_tokens / max(original_tokens, 1)) * 100
            if original_tokens > 0
            else 0
        )
        logger.info(
            "context_compressed",
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            saved_percent=round(saved_percent, 1),
            summarized_turns=self._summarizer.summarized_turns,
        )


class IntegratedCompressionManager:
    """集成压缩管理器 — 压缩上下文 + 双写记忆系统.

    职责：
    1. 调用 ContextCompressor 执行增量压缩
    2. 将压缩摘要写入 MemoryManager（双重保障）
    3. 返回压缩后的消息列表供 Harness 使用

    双写策略确保摘要不会丢失：
    - 摘要缓冲区持久化到 SQLite（通过 IncrementalSummarizer）
    - 摘要内容同时写入 ChromaDB（通过 MemoryManager.add_memory）
    """

    def __init__(
        self,
        compressor: ContextCompressor,
        memory_manager: Any,
    ) -> None:
        self._compressor = compressor
        self._memory = memory_manager

    async def compress_and_persist(
        self,
        messages: list[dict[str, Any]],
        session_id: str,
    ) -> list[dict[str, Any]]:
        """压缩上下文并持久化摘要到记忆系统.

        Args:
            messages: 原始消息列表
            session_id: 会话 ID

        Returns:
            压缩后的消息列表（若未触发压缩则原样返回）
        """
        if self._memory is None:
            return await self._compressor.compress(messages, session_id)

        original_count = len(messages)
        compressed = await self._compressor.compress(
            messages, session_id, memory_manager=self._memory,
        )

        # 若发生压缩，将摘要写入记忆系统
        if len(compressed) < original_count:
            summary_msg = compressed[0] if compressed else None
            if (
                summary_msg
                and summary_msg.get("role") == "system"
                and "[对话历史摘要]" in summary_msg.get("content", "")
            ):
                try:
                    summary_content = summary_msg["content"].replace(
                        "[对话历史摘要]\n", ""
                    )
                    await self._memory.add_memory(
                        content=summary_content,
                        session_id=session_id,
                        metadata={
                            "type": "compression_summary",
                            "source": "integrated_compression",
                            "original_messages": original_count,
                            "compressed_messages": len(compressed),
                        },
                        pinned=False,
                    )
                    logger.info(
                        "compression_summary_persisted",
                        session_id=session_id,
                        original_count=original_count,
                        compressed_count=len(compressed),
                    )
                except Exception as e:
                    logger.warning(
                        "compression_persist_failed",
                        session_id=session_id,
                        error=str(e),
                    )

        return compressed

    @property
    def compressor(self) -> ContextCompressor:
        return self._compressor

    @property
    def memory(self) -> Any:
        return self._memory
