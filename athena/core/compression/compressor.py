"""上下文压缩器 — 增量摘要 + ToolMessage 配对 + 智能保留.

完整压缩流程：
1. 检查是否需要压缩（基于 token 阈值）
2. 识别完整对话轮次（保护 assistant-tool 配对）
3. 分离旧轮次与最近轮次
4. 增量更新摘要（与已有摘要合并）
5. 重建压缩后的消息列表
"""

from __future__ import annotations

from datetime import datetime

from athena.config.settings import Settings
from athena.core.compression.summarizer import ContextSummaryBuffer
from athena.core.llm.provider import LLMProvider
from athena.core.llm.tokens import TokenCounter
from athena.infrastructure.postgre.database import Database
from athena.models import Message, MessageRole
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class ContextCompressor:
    """上下文压缩器."""

    def __init__(
        self,
        llm: LLMProvider,
        token_counter: TokenCounter,
        db: Database,
        settings: Settings,
    ) -> None:
        """

        参数：
            llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            token_counter (TokenCounter): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            db (数据库): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            settings (Settings): 全局配置对象。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._llm = llm
        self._token_counter = token_counter
        self._settings = settings
        self._db = db
        self._max_tokens = self._settings.max_context_tokens
        self._threshold = self._settings.compression_threshold
        self._summary_buffer = ContextSummaryBuffer(
            llm=llm,
            db=db,
            max_summary_tokens=self._settings.max_summary_tokens,
        )
        self._keep_recent_turns = self._settings.keep_recent_turns

    @property
    def summary_buffer(self) -> ContextSummaryBuffer:
        """

        返回值：
            ContextSummaryBuffer: 当前会话的运行时摘要缓冲区。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return self._summary_buffer

    def _should_compress(self, messages: list[Message]) -> bool:
        """检查是否需要压缩（超过阈值时触发）."""
        total = self._estimate_total(messages)
        return total > self._max_tokens * self._threshold

    def _estimate_total(self, messages: list[Message]) -> int:
        """估算消息列表总 token 数."""
        return sum(
            self._token_counter.count_text_tokens(
                f"{message.role.value}: {message.content}"
            )
            for message in messages
        )

    async def compress(
        self,
        messages: list[Message],
        session_id: str | None = None,
    ) -> list[Message]:
        """压缩消息列表.

        增量压缩模式：若 session 记录了上次压缩的消息 ID，只处理该 ID 之后的新消息，
        避免重复处理已压缩的消息，大幅减少送给 LLM 的内容量。

        参数：
            messages: 原始消息列表（完整历史）
            session_id: 会话 ID（用于持久化摘要缓冲区和增量索引）

        返回值：
            压缩后的消息列表（若未触发压缩，则原样返回）
        """
        # 1. 检查是否需要压缩
        if not self._should_compress(messages):
            return messages

        sid = session_id or "_default"

        # 2. 增量模式：获取上次压缩后的增量消息
        incremental_messages = await self._get_incremental_messages(
            messages, session_id
        )

        # 3. 识别完整对话轮次
        turns = self.identify_turns(incremental_messages)
        dialogue_turn_count = sum(
            1 for turn in turns if turn and turn[0].role != MessageRole.SYSTEM
        )
        if dialogue_turn_count == 0:
            return messages

        # 阈值已经触发时，至少摘要一个对话轮次；否则单个超大轮次会
        # 因“最近轮次保留数量”而绕过压缩，无法降低上下文占用。
        keep_recent_turns = min(
            max(self._keep_recent_turns, 0), dialogue_turn_count - 1
        )

        # 4. 分离待摘要轮次和最近轮次
        old_turns, recent_turns = self.split_recent_turns(turns, keep_recent_turns)
        if not old_turns:
            return messages

        # 5. 增量更新摘要
        original_buffer = await self._summary_buffer.get_summary(sid)
        updated_summary = await self._summary_buffer.update_summary(
            old_turns,
            session_id=session_id,
        )

        # 摘要更新失败时降级为简单截断（保留更多消息）
        if not updated_summary and not original_buffer:
            logger.warning("compression_summary_empty_fallback_to_truncation")
            return messages

        # 6. 重建压缩后的消息列表
        compressed = self._rebuild_messages(
            updated_summary,
            recent_turns,
            session_id=session_id or messages[0].session_id,
        )

        # 7. 记录本次压缩的最后一条消息 ID（用于下次增量查询）
        await self._update_last_compressed_id(messages, session_id)

        self._emit_compression_event(messages, compressed)
        return compressed

    async def _get_incremental_messages(
        self,
        all_messages: list[Message],
        session_id: str | None,
    ) -> list[Message]:
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

            # 数据库返回领域消息，保持压缩阶段不依赖 LangChain 类型
            incremental_messages = await self._db.messages.get_after_message(
                session_id, last_compressed_id
            )
            if incremental_messages:
                logger.info(
                    "incremental_messages_loaded",
                    session_id=session_id,
                    total_messages=len(all_messages),
                    incremental_messages=len(incremental_messages),
                )
                return incremental_messages
        except Exception as e:
            logger.warning(
                "incremental_load_failed", error=str(e), session_id=session_id
            )

        return all_messages

    async def _update_last_compressed_id(
        self,
        messages: list[Message],
        session_id: str | None,
    ) -> None:
        """更新 session 表 last_compressed_message_id 字段."""
        if not session_id or not self._db:
            return

        if messages:
            try:
                await self._db.sessions.update(
                    session_id,
                    last_compressed_message_id=messages[-1].id,
                )
            except Exception as e:
                logger.warning(
                    "update_last_compressed_id_failed",
                    error=str(e),
                    session_id=session_id,
                )

    def _rebuild_messages(
        self,
        summary: str,
        recent_turns: list[list[Message]],
        *,
        session_id: str,
    ) -> list[Message]:
        """重建压缩后的消息列表."""
        summary_msg = Message(
            id=f"summary:{session_id}",
            session_id=session_id,
            role=MessageRole.SYSTEM,
            content=f"[对话历史摘要]\n{summary}",
            run_id=f"summary:{session_id}",
            message_type="conversation_summary",
            timestamp=datetime.now(),
        )
        recent_messages: list[Message] = []
        for turn in recent_turns:
            recent_messages.extend(turn)
        return [summary_msg] + recent_messages

    def _emit_compression_event(
        self,
        original: list[Message],
        compressed: list[Message],
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

    def identify_turns(self, messages: list[Message]) -> list[list[Message]]:
        """按 ``run_id``（缺失时按用户消息）分割完整对话轮次.

        返回值：
            轮次列表，每个轮次是一个消息列表
        """
        turns: list[list[Message]] = []
        current_turn: list[Message] = []
        current_run_id: str | None = None

        for msg in messages:
            run_id = msg.run_id
            is_system = msg.role == MessageRole.SYSTEM
            is_user = msg.role == MessageRole.USER
            if is_system and not current_turn:
                turns.append([msg])
                continue
            if (
                current_turn
                and run_id
                and current_run_id
                and str(run_id) != current_run_id
            ):
                turns.append(current_turn)
                current_turn = []
            elif current_turn and not run_id and is_user:
                turns.append(current_turn)
                current_turn = []

            current_turn.append(msg)
            current_run_id = str(run_id) if run_id else None

        if current_turn:
            turns.append(current_turn)

        return turns

    def split_recent_turns(
        self,
        turns: list[list[Message]],
        keep_count: int,
    ) -> tuple[list[list[Message]], list[list[Message]]]:
        """分离旧轮次与最近轮次.

        返回值：
            (旧轮次列表, 最近轮次列表)。
        """
        if keep_count < 0:
            raise ValueError("keep_count 不能为负数")
        if len(turns) <= keep_count:
            return [], turns

        system_indices = {
            index
            for index, turn in enumerate(turns)
            if turn and turn[0].role == MessageRole.SYSTEM
        }
        system_turns = [turns[index] for index in sorted(system_indices)]
        dialogue_turns = [
            turn for index, turn in enumerate(turns) if index not in system_indices
        ]
        if len(dialogue_turns) <= keep_count:
            return [], turns
        if keep_count == 0:
            return dialogue_turns, system_turns
        return dialogue_turns[:-keep_count], system_turns + dialogue_turns[-keep_count:]
