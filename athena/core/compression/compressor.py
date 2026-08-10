"""上下文压缩器 — 增量摘要 + ToolMessage 配对 + 智能保留.

完整压缩流程：
1. 检查是否需要压缩（基于 token 阈值）
2. 识别完整对话轮次（保护 assistant-tool 配对）
3. 分离旧轮次与最近轮次
4. 增量更新摘要（与已有摘要合并）
5. 重建压缩后的消息列表
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, SystemMessage

from athena.config.settings import Settings, get_settings
from athena.core.compression.pairer import MessagePairer
from athena.core.compression.summarizer import IncrementalSummarizer
from athena.core.llm.provider import LLMProvider
from athena.db.database import Database
from athena.utils.logging import get_logger

logger = get_logger(__name__)


from athena.utils.llm import estimate_tokens


def _get_message_id(msg: BaseMessage) -> str | None:
    """从 BaseMessage.metadata 中提取原始消息 ID."""
    meta = getattr(msg, "metadata", None) or {}
    return meta.get("message_id")


class ContextCompressor:
    """上下文压缩器."""

    def __init__(
        self,
        llm: LLMProvider,
        db: Database,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings or get_settings()
        self._db = db
        self._max_tokens = self._settings.max_context_tokens
        self._threshold = self._settings.compression_threshold
        self._pairer = MessagePairer()
        self._summarizer = IncrementalSummarizer(
            llm=llm,
            db=db,
            max_summary_tokens=self._settings.max_summary_tokens,
        )
        self._keep_recent_turns = self._settings.keep_recent_turns

    @property
    def summarizer(self) -> IncrementalSummarizer:
        return self._summarizer

    def _should_compress(self, messages: list[BaseMessage]) -> bool:
        """检查是否需要压缩（超过阈值时触发）."""
        total = 0
        for m in messages:
            total += estimate_tokens(getattr(m, "content", "") or "")
            for tc in getattr(m, "tool_calls", None) or []:
                total += estimate_tokens(str(tc.get("args", {})))
        return total > self._max_tokens * self._threshold

    def _estimate_total(self, messages: list[BaseMessage]) -> int:
        """估算消息列表总 token 数."""
        return sum(estimate_tokens(getattr(m, "content", "") or "") for m in messages)

    async def compress(
        self,
        messages: list[BaseMessage],
        session_id: str | None = None,
    ) -> list[BaseMessage]:
        """压缩消息列表.

        增量压缩模式：若 session 记录了上次压缩的消息 ID，只处理该 ID 之后的新消息，
        避免重复处理已压缩的消息，大幅减少送给 LLM 的内容量。

        Args:
            messages: 原始消息列表（完整历史）
            session_id: 会话 ID（用于持久化摘要缓冲区和增量索引）

        Returns:
            压缩后的消息列表（若未触发压缩，则原样返回）
        """
        # 1. 检查是否需要压缩
        if not self._should_compress(messages):
            return messages

        sid = session_id or "_default"

        # 2. 增量模式：获取上次压缩后的增量消息
        incremental_messages = await self._get_incremental_messages(messages, session_id)

        # 3. 识别完整对话轮次
        turns = self._pairer.identify_turns(incremental_messages)

        if len(turns) <= self._keep_recent_turns + 1:
            return messages

        # 4. 分离旧轮次和最近轮次
        old_turns, recent_turns = self._pairer.get_recent_turns(turns, self._keep_recent_turns)
        if not old_turns:
            return messages

        # 5. 增量更新摘要
        original_buffer = await self._summarizer.get_summary(sid)
        updated_summary = await self._summarizer.update_summary(
            old_turns,
            session_id=session_id,
        )

        # 摘要更新失败时降级为简单截断（保留更多消息）
        if not updated_summary and not original_buffer:
            logger.warning("compression_summary_empty_fallback_to_truncation")
            return messages

        # 6. 重建压缩后的消息列表
        compressed = self._rebuild_messages(updated_summary, recent_turns)

        # 7. 记录本次压缩的最后一条消息 ID（用于下次增量查询）
        await self._update_last_compressed_id(messages, session_id)

        self._emit_compression_event(messages, compressed)
        return compressed

    async def _get_incremental_messages(
        self,
        all_messages: list[BaseMessage],
        session_id: str | None,
    ) -> list[BaseMessage]:
        """获取增量消息：若存在上次压缩记录，只返回该记录之后的消息."""
        if not session_id or not self._db:
            return all_messages

        try:
            session = await self._db.sessions.get(session_id)
            if not session:
                return all_messages

            last_compressed_id = session.last_compressed_message_id
            if not last_compressed_id:
                return all_messages

            # 从数据库查询增量消息，转换为 BaseMessage
            incremental_messages = await self._db.messages.get_after_message(session_id, last_compressed_id)
            if incremental_messages:
                from athena.utils.message import dicts_to_messages
                incremental = dicts_to_messages(incremental_messages)
                logger.info(
                    "incremental_messages_loaded",
                    session_id=session_id,
                    total_messages=len(all_messages),
                    incremental_messages=len(incremental),
                )
                return incremental
        except Exception as e:
            logger.warning("incremental_load_failed", error=str(e), session_id=session_id)

        return all_messages

    async def _update_last_compressed_id(
        self,
        messages: list[BaseMessage],
        session_id: str | None,
    ) -> None:
        """更新 session 表 last_compressed_message_id 字段."""
        if not session_id or not self._db:
            return

        # 从后往前找最后一条有原始 ID 的消息
        last_id = None
        for msg in reversed(messages):
            last_id = _get_message_id(msg)
            if last_id:
                break

        if last_id:
            try:
                await self._db.sessions.update(
                    session_id,
                    last_compressed_message_id=last_id,
                )
            except Exception as e:
                logger.warning("update_last_compressed_id_failed", error=str(e), session_id=session_id)

    def _rebuild_messages(
        self,
        summary: str,
        recent_turns: list[list[BaseMessage]],
    ) -> list[BaseMessage]:
        """重建压缩后的消息列表."""
        summary_msg = SystemMessage(
            content=f"[对话历史摘要]\n{summary}",
            metadata={"type": "conversation_summary", "is_incremental": True},
        )
        recent_messages: list[BaseMessage] = []
        for turn in recent_turns:
            recent_messages.extend(turn)
        return [summary_msg] + recent_messages

    def _emit_compression_event(
        self,
        original: list[BaseMessage],
        compressed: list[BaseMessage],
    ) -> None:
        """发送压缩统计事件（供调用方推送）."""
        original_tokens = self._estimate_total(original)
        compressed_tokens = self._estimate_total(compressed)
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
        )
