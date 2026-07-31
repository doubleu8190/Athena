"""增量摘要生成器 — 维护持续更新的摘要缓冲区.

每次压缩时只处理新增内容，与已有摘要合并，避免重复处理。
_summary_buffer 需持久化到记忆系统（session_id 绑定）并在启动时恢复。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage

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
    """增量摘要生成器."""

    BUFFER_METADATA_KEY = "compression_summary_buffer"

    def __init__(self, llm: Any, max_summary_tokens: int = 2000) -> None:
        self._llm = llm
        self._max_summary_tokens = max_summary_tokens
        self._summary_buffer: str = ""
        self._summarized_turns: int = 0

    @property
    def summary_buffer(self) -> str:
        return self._summary_buffer

    @property
    def summarized_turns(self) -> int:
        return self._summarized_turns

    def set_buffer(self, buffer: str, summarized_turns: int = 0) -> None:
        """恢复持久化的摘要缓冲区（启动时调用）."""
        self._summary_buffer = buffer
        self._summarized_turns = summarized_turns
        logger.info(
            "summary_buffer_restored",
            buffer_tokens=len(buffer) // 4,
            summarized_turns=summarized_turns,
        )

    async def update_summary(
        self,
        old_turns: list[list[dict[str, Any]]],
        session_id: str | None = None,
        memory_manager: Any | None = None,
    ) -> str:
        """增量更新摘要.

        Args:
            old_turns: 需摘要的旧轮次
            session_id: 会话 ID（用于持久化到记忆系统）
            memory_manager: 记忆管理器（可选，用于持久化摘要缓冲区）

        Returns:
            更新后的完整摘要文本
        """
        if not old_turns:
            return self._summary_buffer

        new_content = self._format_turns_for_summary(old_turns)
        existing_section = ""
        if self._summary_buffer:
            existing_section = f"历史摘要：\n{self._summary_buffer}\n\n"

        prompt = SUMMARY_PROMPT.format(
            existing_summary_section=existing_section,
            new_messages=new_content,
        )

        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = getattr(response, "content", str(response))
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            self._summary_buffer = content.strip()
        except Exception as e:
            logger.error("incremental_summary_failed", error=str(e))
            # 失败时保留旧摘要，不更新
            return self._summary_buffer

        self._summarized_turns += len(old_turns)

        # 持久化到记忆系统（project_memory 约束：session_id 绑定）
        if memory_manager is not None and session_id:
            await self._persist_buffer(session_id, memory_manager)

        return self._summary_buffer

    async def _persist_buffer(self, session_id: str, memory_manager: Any) -> None:
        """持久化摘要缓冲区到记忆系统."""
        try:
            await memory_manager.add_memory(
                content=self._summary_buffer,
                session_id=session_id,
                metadata={
                    "type": self.BUFFER_METADATA_KEY,
                    "summarized_turns": self._summarized_turns,
                    "source": "compression",
                },
                pinned=True,
            )
        except Exception as e:
            logger.warning("summary_buffer_persist_failed", error=str(e))

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

    def get_summary(self) -> str:
        return self._summary_buffer

    def reset(self) -> None:
        """重置摘要（新会话开始时调用）."""
        self._summary_buffer = ""
        self._summarized_turns = 0
