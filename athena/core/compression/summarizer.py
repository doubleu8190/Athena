"""增量摘要生成器 — 维护持续更新的摘要缓冲区.

每次压缩时只处理新增内容，与已有摘要合并，避免重复处理。
_summary_buffer 按 session_id 隔离，避免跨会话数据污染。
缓冲区持久化到 session metadata（session_id 绑定）并可在启动时恢复。
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from athena.core.llm.provider import LLMProvider
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.db.database import Database

logger = get_logger(__name__)

SUMMARY_PROMPT = """# 角色
你是一个简洁、客观的累积摘要引擎。

# 核心规则
- **优先级层级**（压缩时）：决策 > 未解决问题 > 用户偏好 > 工具输出 > 事实陈述。超出长度限制时，优先丢弃最低优先级的内容。
- **冲突解决**：若新信息与历史摘要矛盾，用新信息**覆盖**旧内容，并显式标注更新（如"此前为 X，现更正为 Y"）。
- **禁止幻觉**：严格仅从提供的文本中合成摘要，不得添加外部知识、推测或解读。
- **去重**：若同一事实同时出现在历史摘要和新消息中，仅保留一次，采用最新表述。

# 长度控制
- 内容超过 550 词时，按优先级逆序逐步移除：
  1. 移除事实陈述
  2. 移除工具输出（仅保留关键结果）
  3. 移除用户偏好
  4. 移除未解决问题
  5. 将决策压缩为要点
- 始终保留关键标识符（ID、时间戳、状态码）。

# 结构化数据处理
处理工具输出时：
- **JSON**：提取关键值（状态、ID、数量），丢弃冗余元数据。
- **日志**：仅提取错误信息和时间戳。
- **表格**：总结关键趋势或标出异常值。
- **代码**：概括目的和核心逻辑，不保留完整实现。

# 必须保留的关键数据
始终保留以下信息：
- **时间戳和截止日期**（如"截止日期：2026 年 3 月"、"计划于 Q3"）
- **数值**（数量、大小、百分比、版本号）
- **状态码和错误信息**（如"HTTP 404"、"超时错误"）
- **文件路径和 URL**（关键位置和引用）
- **提及的工具、服务或技术名称**（如"PostgreSQL"、"LangGraph"）
- **决策结果**（决定了什么及原因）

# 输出格式
- **文体**：使用清晰连贯的段落（非要点列表）。
- **结构**：按主题聚类组织（如"决策"、"待处理事项"、"上下文"），而非按时间顺序，以提升可读性。
- **长度目标**：300–500 词。
  - 若新增信息较少，压缩至约 200 词——不要刻意填充。
  - 若新增信息密集，硬上限 550 词——按优先级层级裁剪。
- **语气**：中性、客观、新闻式。避免使用评价性形容词（如"重要的"、"令人惊讶的"）。

# 边界情况
- 若**无新消息**，直接原样输出历史摘要。
- 若**工具输出**包含大量原始数据（日志、JSON），仅提取关键值（状态码、ID、错误信息、最终结果），丢弃冗长的堆栈跟踪。

---

{existing_summary_section}
新增对话内容：
{new_messages}

**最终规则**：若用户要求调整风格（如"简短一些"），覆盖长度目标，但绝不违反冲突解决和禁止幻觉规则。"""


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
            session = await self._db.get_session(session_id)
            if not session:
                return

            summary = session.compression_summary
            if summary:
                self._buffers[session_id] = summary
                logger.info(
                    "summary_buffer_lazy_loaded",
                    session_id=session_id,
                    buffer_tokens=len(summary) // 4,
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
            buffer_tokens=len(buffer) // 4,
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
            await self._db.update_session(
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
