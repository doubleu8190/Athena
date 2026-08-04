"""增量摘要生成器 — 维护持续更新的摘要缓冲区.

每次压缩时只处理新增内容，与已有摘要合并，避免重复处理。
_summary_buffer 按 session_id 隔离，避免跨会话数据污染。
缓冲区持久化到记忆系统（session_id 绑定）并可在启动时恢复。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage

from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.utils.logging import get_logger

logger = get_logger(__name__)

SUMMARY_PROMPT = """你需要将以下对话历史压缩为简洁的摘要。

{existing_summary_section}
新增对话内容：
{new_messages}

摘要要求：
- 合并历史摘要和新增内容，生成完整摘要
- 保留用户的核心需求和决策
- 保留所有工具调用的关键结果
- 保留未完成的任务和待处理事项
- 保留重要的代码片段和配置信息
- 压缩到尽可能简洁，同时保持信息完整性"""


class IncrementalSummarizer:
    """增量摘要生成器 — 按 session_id 隔离摘要缓冲区."""

    BUFFER_METADATA_KEY = "compression_summary_buffer"

    def __init__(self, llm: LLMProvider, memory_manager: MemoryManager, max_summary_tokens: int = 2000) -> None:
        self._llm = llm
        self._max_summary_tokens = max_summary_tokens
        self._memory_manager = memory_manager
        # 按 session_id 隔离，避免跨会话数据污染
        self._buffers: dict[str, str] = {}
        self._summarized_turns: dict[str, int] = {}

    def _get_buffer(self, session_id: str) -> str:
        """获取指定会话的摘要缓冲区."""
        return self._buffers.get(session_id, "")

    def _get_turns(self, session_id: str) -> int:
        """获取指定会话的已摘要轮次数."""
        return self._summarized_turns.get(session_id, 0)

    def get_summary(self, session_id: str) -> str:
        """获取指定会话的当前摘要."""
        return self._get_buffer(session_id)

    def get_summarized_turns(self, session_id: str) -> int:
        """获取指定会话的已摘要轮次数."""
        return self._get_turns(session_id)

    def set_buffer(self, session_id: str, buffer: str, summarized_turns: int = 0) -> None:
        """恢复持久化的摘要缓冲区（启动时调用）."""
        self._buffers[session_id] = buffer
        self._summarized_turns[session_id] = summarized_turns
        logger.info(
            "summary_buffer_restored",
            session_id=session_id,
            buffer_tokens=len(buffer) // 4,
            summarized_turns=summarized_turns,
        )

    def reset(self, session_id: str) -> None:
        """重置指定会话的摘要（新会话或会话结束时调用）."""
        self._buffers.pop(session_id, None)
        self._summarized_turns.pop(session_id, None)

    async def update_summary(
        self,
        old_turns: list[list[dict[str, Any]]],
        session_id: str | None = None,
    ) -> str:
        """增量更新摘要.

        Args:
            old_turns: 需摘要的旧轮次
            session_id: 会话 ID（用于隔离缓冲区和持久化到记忆系统）
            memory_manager: 记忆管理器（可选，用于持久化摘要缓冲区）

        Returns:
            更新后的完整摘要文本
        """
        if not old_turns:
            return "" if session_id is None else self._get_buffer(session_id)

        sid = session_id or "_default"
        current_buffer = self._get_buffer(sid)

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
            content = getattr(response, "content", str(response))
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            self._buffers[sid] = content.strip()
        except Exception as e:
            logger.error("incremental_summary_failed", error=str(e), session_id=sid)
            # 失败时保留旧摘要，不更新
            return current_buffer

        self._summarized_turns[sid] = self._get_turns(sid) + len(old_turns)

        # 持久化到记忆系统（project_memory 约束：session_id 绑定）
        if session_id:
            await self._persist_buffer(session_id)

        return self._buffers[sid]

    async def _persist_buffer(self, session_id: str) -> None:
        """持久化摘要缓冲区到记忆系统."""
        try:
            await self._memory_manager.add_memory(
                content=self._get_buffer(session_id),
                session_id=session_id,
                metadata={
                    "type": self.BUFFER_METADATA_KEY,
                    "summarized_turns": self._get_turns(session_id),
                    "source": "compression",
                },
                pinned=True,
            )
        except Exception as e:
            logger.warning("summary_buffer_persist_failed", error=str(e), session_id=session_id)

    def _format_turns_for_summary(self, turns: list[list[dict[str, Any]]]) -> str:
        """将轮次格式化为摘要输入."""
        formatted: list[str] = []
        for i, turn in enumerate(turns, 1):
            turn_content: list[str] = []
            for msg in turn:
                role = msg.get("role", "")
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])
                if role == "user":
                    turn_content.append(f"用户: {content}")
                elif role == "assistant":
                    if tool_calls:
                        calls_desc = "; ".join(
                            f"{tc.get('name', '')}({json.dumps(tc.get('args', {}), ensure_ascii=False)})"
                            for tc in tool_calls
                        )
                        turn_content.append(f"助手: {content}\n  调用工具: {calls_desc}")
                    else:
                        turn_content.append(f"助手: {content}")
                elif role == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    turn_content.append(f"工具结果 [{tc_id}]: {content[:200]}")
            formatted.append(f"--- 轮次 {i} ---\n" + "\n".join(turn_content))
        return "\n\n".join(formatted)
