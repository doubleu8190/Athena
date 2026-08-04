"""上下文压缩器 — 增量摘要 + ToolMessage 配对 + 智能保留.

完整压缩流程：
1. 检查是否需要压缩（基于 token 阈值）
2. 识别完整对话轮次（保护 assistant-tool 配对）
3. 分离旧轮次与最近轮次
4. 增量更新摘要（与已有摘要合并）
5. 重建压缩后的消息列表
"""

from __future__ import annotations

from athena.config.settings import Settings, get_settings
from athena.core.compression.pairer import MessagePairer
from athena.core.compression.summarizer import IncrementalSummarizer
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.types import JSONValue
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class ContextSizeChecker:
    """上下文大小检测器."""

    def __init__(self, max_context_tokens: int = 128000, threshold: float = 0.8) -> None:
        self._max_tokens = max_context_tokens
        self._threshold = threshold

    async def should_compress(self, messages: list[dict[str, JSONValue]]) -> bool:
        """检查是否需要压缩（超过阈值时触发）."""
        total = sum(self._estimate_tokens(m.get("content", "")) for m in messages)
        # 也计入 tool_calls 的 token
        for m in messages:
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                total += self._estimate_tokens(str(tc.get("args", {})))
        return total > self._max_tokens * self._threshold

    def estimate_total(self, messages: list[dict[str, JSONValue]]) -> int:
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
        llm: LLMProvider,
        memory_manager: MemoryManager,
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
            memory_manager=memory_manager,
            max_summary_tokens=self._settings.max_summary_tokens,
        )
        self._keep_recent_turns = self._settings.keep_recent_turns

    @property
    def summarizer(self) -> IncrementalSummarizer:
        return self._summarizer

    async def compress(
        self,
        messages: list[dict[str, JSONValue]],
        session_id: str | None = None,
    ) -> list[dict[str, JSONValue]]:
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
        sid = session_id or "_default"
        original_buffer = self._summarizer.get_summary(sid)
        updated_summary = await self._summarizer.update_summary(
            old_turns,
            session_id=session_id,
        )

        # 摘要更新失败时降级为简单截断（保留更多消息）
        if not updated_summary and not original_buffer:
            logger.warning("compression_summary_empty_fallback_to_truncation")
            return messages

        # 5. 重建压缩后的消息列表
        compressed = self._rebuild_messages(updated_summary, recent_turns)

        await self._emit_compression_event(messages, compressed, sid)
        return compressed

    def _rebuild_messages(
        self,
        summary: str,
        recent_turns: list[list[dict[str, JSONValue]]],
    ) -> list[dict[str, JSONValue]]:
        """重建压缩后的消息列表."""
        summary_msg = {
            "role": "system",
            "content": f"[对话历史摘要]\n{summary}",
            "metadata": {
                "type": "conversation_summary",
                "is_incremental": True,
            },
        }
        recent_messages: list[dict[str, JSONValue]] = []
        for turn in recent_turns:
            recent_messages.extend(turn)
        return [summary_msg] + recent_messages

    async def _emit_compression_event(
        self,
        original: list[dict[str, JSONValue]],
        compressed: list[dict[str, JSONValue]],
        session_id: str,
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
            summarized_turns=self._summarizer.get_summarized_turns(session_id),
        )