"""增量摘要生成器 — 维护持续更新的摘要缓冲区.

每次压缩时只处理新增内容，与已有摘要合并，避免重复处理。
_summary_buffer 按 session_id 隔离，避免跨会话数据污染。
缓冲区持久化到 session metadata（session_id 绑定）并可在启动时恢复。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from athena.core.llm.provider import LLMProvider
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

if TYPE_CHECKING:
    from athena.infrastructure.sqlite.database import Database

logger = get_logger(__name__)

SUMMARY_PROMPT = get_prompt("compression_summary")


class IncrementalSummarizer:
    """增量摘要生成器 — 按 session_id 隔离摘要缓冲区."""

    def __init__(self, llm: LLMProvider, db: Database, max_summary_tokens: int = 2000) -> None:
        self._llm = llm
        self._max_summary_tokens = max_summary_tokens
        self._db = db
        # 按 session_id 隔离，避免跨会话数据污染
        self._buffers: dict[str, str] = {}

    def _get_buffer_local(self, session_id: str) -> str:
        """获取内存中的摘要缓冲区（不触发懒加载）."""
        return self._buffers.get(session_id, "")

    async def _load_buffer(self, session_id: str) -> None:
        """从 session 表懒加载摘要缓冲区."""
        try:
            session = await self._db.sessions.get(session_id)
            if not session:
                return

            summary = session.compression_summary
            if summary:
                self._buffers[session_id] = summary
                logger.info(
                    "summary_buffer_lazy_loaded",
                    session_id=session_id,
                    buffer_tokens=self._llm.count_text_tokens(summary),
                )
        except Exception as e:
            logger.warning("summary_buffer_load_failed", error=str(e), session_id=session_id)

    async def get_summary(self, session_id: str) -> str:
        """获取指定会话的当前摘要（懒加载：内存无缓存时从 session 表恢复）."""
        if session_id not in self._buffers:
            await self._load_buffer(session_id)
        return self._buffers.get(session_id, "")

    def set_buffer(self, session_id: str, buffer: str) -> None:
        """直接设置摘要缓冲区（用于测试或外部恢复）."""
        self._buffers[session_id] = buffer
        logger.info(
            "summary_buffer_set",
            session_id=session_id,
            buffer_tokens=self._llm.count_text_tokens(buffer),
        )

    def reset(self, session_id: str) -> None:
        """重置指定会话的摘要（新会话或会话结束时调用）."""
        self._buffers.pop(session_id, None)

    async def update_summary(
        self,
        old_turns: list[list[BaseMessage]],
        session_id: str | None = None,
    ) -> str:
        """增量更新摘要.

        Args:
            old_turns: 需摘要的旧轮次
            session_id: 会话 ID（用于隔离缓冲区和持久化到 session 表）

        Returns:
            更新后的完整摘要文本
        """
        if not old_turns:
            return "" if session_id is None else await self.get_summary(session_id)

        sid = session_id or "_default"
        current_buffer = await self.get_summary(sid)

        new_content = self._format_turns_for_summary(old_turns)
        existing_section = ""
        if current_buffer:
            existing_section = f"历史摘要：\n{current_buffer}\n\n"

        prompt = SUMMARY_PROMPT.format(
            existing_summary_section=existing_section,
            new_messages=new_content,
        )

        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = extract_message_text(response).strip()
            self._buffers[sid] = content
        except Exception as e:
            logger.error("incremental_summary_failed", error=str(e), session_id=sid)
            # 失败时保留旧摘要，不更新
            return current_buffer

        # 持久化到 session metadata
        if session_id:
            await self._persist_buffer(session_id)

        return self._buffers[sid]

    async def _persist_buffer(self, session_id: str) -> None:
        """持久化摘要缓冲区到 session 表 compression_summary 字段."""
        try:
            await self._db.sessions.update(
                session_id,
                compression_summary=self._get_buffer_local(session_id),
            )
        except Exception as e:
            logger.warning("summary_buffer_persist_failed", error=str(e), session_id=session_id)

    def _format_turns_for_summary(self, turns: list[list[BaseMessage]]) -> str:
        """将轮次格式化为摘要输入."""
        formatted: list[str] = []
        for i, turn in enumerate(turns, 1):
            turn_content: list[str] = []
            for msg in turn:
                content = getattr(msg, "content", "") or ""
                if isinstance(msg, HumanMessage):
                    turn_content.append(f"用户: {content}")
                elif isinstance(msg, AIMessage):
                    tool_calls = getattr(msg, "tool_calls", None) or []
                    if tool_calls:
                        calls_desc = "; ".join(
                            f"{tc.get('name', '')}({json.dumps(tc.get('args', {}), ensure_ascii=False)})"
                            for tc in tool_calls
                        )
                        turn_content.append(f"助手: {content}\n  调用工具: {calls_desc}")
                    else:
                        turn_content.append(f"助手: {content}")
                elif isinstance(msg, ToolMessage):
                    tc_id = getattr(msg, "tool_call_id", "")
                    turn_content.append(f"工具结果 [{tc_id}]: {content[:500]}")
            formatted.append(f"--- 轮次 {i} ---\n" + "\n".join(turn_content))
        return "\n\n".join(formatted)
