"""记忆摘要与事实提取 — ConversationSummarizer + FactExtractor.

- FactExtractor: 使用 LLM 结构化输出从消息中提取事实（带 fallback）
- ConversationSummarizer: 达到阈值轮数时生成对话摘要并写入记忆
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, ValidationError

from athena.config.settings import Settings, get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class Fact(BaseModel):
    """提取的事实/实体."""

    subject: str = Field(description="主体")
    predicate: str = Field(description="关系/属性")
    object: str = Field(description="客体/值")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    category: str = Field(default="fact", description="分类: preference/fact/entity/event")


class FactList(BaseModel):
    """事实列表（用于 with_structured_output）."""

    facts: list[Fact] = Field(default_factory=list)


class FactExtractor:
    """从对话消息中提取事实和实体."""

    EXTRACTION_PROMPT = """请从以下消息中提取关键事实和实体，每条事实包含主体、关系、客体、置信度和分类。

消息: {message}

分类参考:
- preference: 用户偏好或习惯
- fact: 客观事实
- entity: 实体（项目/人/组织等）
- event: 事件

只提取明确的信息，不要臆测。无事实可提取时返回空列表。"""

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def extract(self, message: str, session_id: str, memory_manager: MemoryManager) -> list[Fact]:
        """使用 LLM 从消息中提取事实并写入记忆."""
        facts = await self._extract_facts(message)
        for fact in facts:
            content = f"{fact.subject} {fact.predicate} {fact.object}"
            try:
                await memory_manager.add_memory(
                    content=content,
                    session_id=session_id,
                    metadata={
                        "type": "fact",
                        "category": fact.category,
                        "confidence": fact.confidence,
                        "source": "extraction",
                    },
                )
            except Exception as e:
                logger.warning("fact_save_failed", error=str(e))
        return facts

    async def _extract_facts(self, message: str) -> list[Fact]:
        """调用 LLM 提取事实，带 fallback 机制（project_memory 约束）."""
        prompt = self.EXTRACTION_PROMPT.format(message=message)
        # 优先尝试结构化输出
        try:
            structured = self._llm.with_structured_output(FactList)
            result = await structured.ainvoke([HumanMessage(content=prompt)])
            if isinstance(result, FactList):
                return result.facts
            if isinstance(result, dict):
                return FactList.model_validate(result).facts
        except (ValidationError, Exception) as e:
            logger.warning("structured_fact_extraction_failed", error=str(e))

        # Fallback: 直接 ainvoke 并解析
        try:
            import json
            import re
            response = await self._llm.ainvoke([HumanMessage(content=prompt + "\n\n请以 JSON 格式返回：{\"facts\": [...]}")])
            content = getattr(response, "content", str(response))
            if isinstance(content, list):
                # 多模态消息：拼接文本部分
                content = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                return []
            data = json.loads(match.group(0))
            return FactList.model_validate(data).facts
        except Exception as e:
            logger.warning("fallback_fact_extraction_failed", error=str(e))
            return []


class ConversationSummarizer:
    """对话摘要生成器."""

    SUMMARY_PROMPT = """请对以下对话进行摘要，提取关键信息和决策：

{conversation}

返回格式：
- 关键事实列表
- 用户偏好
- 未完成的任务
- 重要决策

摘要应简洁但保留所有关键信息。"""

    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_manager: MemoryManager,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm_provider
        self._memory = memory_manager
        self._settings = settings or get_settings()
        self._summary_threshold = self._settings.summary_threshold

    async def summarize_if_needed(
        self,
        session_id: str,
        turn_count: int,
        messages: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """达到阈值时生成摘要并写入记忆."""
        if turn_count <= 0 or turn_count % self._summary_threshold != 0:
            return None

        conversation = self._format_messages(messages or [])
        if not conversation.strip():
            return None

        try:
            response = await self._llm.ainvoke([
                HumanMessage(content=self.SUMMARY_PROMPT.format(conversation=conversation))
            ])
            content = getattr(response, "content", str(response))
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            summary_text = content.strip()
        except Exception as e:
            logger.error("summary_generation_failed", error=str(e))
            return None

        try:
            await self._memory.add_memory(
                content=summary_text,
                session_id=session_id,
                metadata={
                    "type": "summary",
                    "turn_count": turn_count,
                    "source": "threshold",
                },
                pinned=False,
            )
            logger.info("conversation_summary_saved", session_id=session_id, turn_count=turn_count)
        except Exception as e:
            logger.warning("summary_save_failed", error=str(e))

        return summary_text

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role and content:
                parts.append(f"{role}: {content}")
        return "\n".join(parts)
