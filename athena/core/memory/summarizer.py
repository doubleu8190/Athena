"""记忆摘要与事实提取 — ConversationSummarizer + FactExtractor.

- FactExtractor: 从消息中提取原子事实（key/value，提示词声明纯 JSON 格式，
  带已有记忆语义去重）
- ConversationSummarizer: 达到阈值轮数时生成结构化对话摘要并写入记忆
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from athena.config.settings import Settings, get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.models import Message
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class AtomicFact(BaseModel):
    """原子事实（key/value 结构）."""

    key: str = Field(description="简短标识符，snake_case，≤30 字符")
    value: str = Field(description="事实内容，自包含，≤100 字符")
    category: str = Field(
        default="fact",
        description=(
            "分类: preference/profile/project/technical_decision/"
            "fact/solution/unresolved/other"
        ),
    )
    confidence: float = Field(
        default=0.8, ge=0.0, le=1.0, description="置信度 0-1，低于 0.6 不输出"
    )


class AtomicFactList(BaseModel):
    """原子事实列表（用于 with_structured_output）."""

    atomic_facts: list[AtomicFact] = Field(default_factory=list)


class FactExtractor:
    """从对话消息中提取原子事实和实体."""

    EXTRACTION_PROMPT = """# 角色
你是一个精确、保守的信息提取引擎。你的任务是从对话中提炼具有长期价值的信息，
并严格避免与已有记忆重复。

# 输入
- `incomplete_notice`: 指示对话是否可能未结束的标记（如"对话尚未结束"或空字符串）。
- `existing_memories`: 之前提取的原子事实与摘要列表。任何与已有记忆语义等价的内容
  都不得再次提取。
- `conversation_text`: 待提取的对话内容（用户与助手消息）。

incomplete_notice: {incomplete_notice}

existing_memories:
{existing_memories}

conversation_text:
{conversation_text}

# 提取决策树
1. 如果对话仅包含问候、闲聊或琐碎交流 → 输出空 JSON（{{"atomic_facts": []}}）。
2. 如果 `incomplete_notice` 表示对话未结束 → 只提取完全确认的事实
   （明确陈述且非假设），跳过未决决策或未成型的提议。
3. 否则 → 按下列规则正常提取。

# 提取规则

## A. 提取什么（长期价值）
只提取对未来交互可能有用的信息，包括：
- 用户偏好（如"偏好简洁回答"、"不喜欢 Python"）
- 个人画像（如"住在东京"、"是后端工程师"）
- 技术决策（如"选择 PostgreSQL 而非 MySQL"、"采用微服务架构"）
- 项目背景（如"仓库名: my-app"、"截止时间: 2026 Q3"）
- 问题解决方案（如"通过增大 keepalive 修复超时"）
- 需要跟进而未解决的问题（如"需要决定云服务商"）

不要提取：
- 一次性问答，之后不会再被引用
- 客套话或填充内容
- 临时状态（如"现在感觉很累"）

## B. 原子事实
每条原子事实是独立、自包含、可验证的信息。
示例：{{"key": "timezone", "value": "UTC+8", "category": "profile"}}。

## C. 避免重复（关键）
- 添加任何新条目前，先与所有 `existing_memories` 做语义比较。
- 若已有记忆存在（即使措辞略有不同），跳过该提取。
- 若新信息更新了已有记忆（如偏好从"Python"改为"Rust"），作为新事实提取，
  并在 value 中标注更新来源，如 "Rust (was Python)"。

## D. 置信度 (0-1)
按证据强度评分：
- 1.0: 用户明确清晰陈述
- 0.8-0.9: 由多次表述强推定
- 0.6-0.7: 从上下文推断但未明确确认
- < 0.6: 丢弃，不输出

不确定时倾向于更低的置信度。

## E. 类别
- preference: 用户偏好或选择
- profile: 个人信息或特征
- project: 项目相关背景
- technical_decision: 技术选型或架构决策
- fact: 一般事实
- solution: 问题解决方案或修复
- unresolved: 待解决的问题或决策
- other: 其他

# 输出限制
- 每次提取最多 20 条原子事实
- 每条 key ≤ 30 字符（snake_case）
- 每条 value ≤ 100 字符

# 输出格式
必须输出纯 JSON，不能有 markdown 代码围栏或额外文字。使用如下精确结构，
所有字段必填，无内容时返回空数组。

{{
  "atomic_facts": [
    {{
      "key": "short_identifier",
      "value": "fact content",
      "category": "preference",
      "confidence": 0.9
    }}
  ]
}}

# 示例
输入:
conversation_text:
[用户] 我把新项目从 Python 换成了 Rust。
[助手] 好选择！Rust 在内存安全方面很棒。
[用户] 是的，我们目标 2026 Q3 发布。

输出:
{{
  "atomic_facts": [
    {{"key": "language", "value": "Rust (was Python)", "category": "preference", "confidence": 1.0}},
    {{"key": "release_date", "value": "2026 Q3", "category": "project", "confidence": 1.0}}
  ]
}}"""

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def extract(
        self, message: str, session_id: str, memory_manager: MemoryManager
    ) -> list[AtomicFact]:
        """使用 LLM 从消息中提取原子事实并写入记忆."""
        existing = await self._fetch_existing_memories(
            memory_manager, message, session_id
        )
        facts = await self._extract_facts(message, existing)
        for fact in facts:
            content = f"{fact.key}: {fact.value}"
            try:
                await memory_manager.add_memory(
                    content=content,
                    metadata={
                        "session_id": session_id,
                        "type": "fact",
                        "category": fact.category,
                        "confidence": fact.confidence,
                        "source": "extraction",
                    },
                )
            except Exception as e:
                logger.warning("fact_save_failed", error=str(e))
        return facts

    async def _fetch_existing_memories(
        self,
        memory_manager: MemoryManager,
        message: str,
        session_id: str,
        limit: int = 8,
    ) -> str:
        """检索相关已有记忆，供提示词做语义去重."""
        try:
            results = await memory_manager.search(
                query=message, n_results=limit, where={"session_id": session_id}
            )
        except Exception as e:
            logger.warning("existing_memory_fetch_failed", error=str(e))
            return "(无已有记忆)"
        if not results:
            return "(无已有记忆)"
        lines: list[str] = []
        for r in results:
            content = r.get("content", "")
            meta = r.get("metadata") or {}
            cat = meta.get("category") or meta.get("type") or ""
            lines.append(f"- {content} (category: {cat})" if cat else f"- {content}")
        return "\n".join(lines)

    async def _extract_facts(
        self, conversation_text: str, existing_memories: str
    ) -> list[AtomicFact]:
        """调用 LLM 提取原子事实，提示词已声明纯 JSON 返回格式，直接解析.

        单一 ainvoke 路径兼容不支持结构化输出的 Provider（如 Ollama/DeepSeek），
        且 ainvoke 会经过重试管理器（with_structured_output 不会）。
        """
        prompt = self.EXTRACTION_PROMPT.format(
            incomplete_notice="",
            existing_memories=existing_memories,
            conversation_text=conversation_text,
        )
        try:
            import json
            import re

            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = extract_message_text(response)
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                return []
            data = json.loads(match.group(0))
            return AtomicFactList.model_validate(data).atomic_facts
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))
            return []


class Summary(BaseModel):
    """对话摘要条目."""

    topic: str = Field(description="讨论主题的简短标签")
    content: str = Field(description="摘要正文，2-3 句话，≤300 字符")
    category: str = Field(
        default="technical_discussion",
        description=(
            "分类: technical_discussion/problem_solving/planning/"
            "decision/open_discussion/other"
        ),
    )
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class SummaryList(BaseModel):
    """摘要列表（用于 with_structured_output）."""

    summaries: list[Summary] = Field(default_factory=list)


class ConversationSummarizer:
    """对话摘要生成器."""

    SUMMARIES_PROMPT = """# 角色
你是对话摘要引擎，把一段对话提炼为连贯的叙事摘要，保留关键上下文、决策与理由，
仅保留具有长期价值的内容。

# 输入
{conversation}

# 提取规则
- 每条摘要对应一个讨论主线或问题解决过程，保留关键上下文、决策与理由。
- 如果讨论明显未完成，不要当作"决策"总结，可标注为 open_discussion 并给出较低置信度。
- 只总结有长期价值的内容，忽略客套与一次性问答。

# 摘要类别
- technical_discussion: 技术话题讨论
- problem_solving: 问题分析与解决过程
- planning: 规划或路线图讨论
- decision: 决策过程
- open_discussion: 未完成或进行中的讨论
- other: 其他

# 输出限制
- 每次最多 5 条摘要
- topic 为简短标签
- content 为 2-3 句话，≤300 字符

# 输出格式
必须输出纯 JSON，不能有 markdown 代码围栏或额外文字。所有字段必填，
无内容时返回空数组。

{{
  "summaries": [
    {{
      "topic": "discussion topic",
      "content": "summary content",
      "category": "technical_discussion",
      "confidence": 0.85
    }}
  ]
}}

# 示例
输入:
[用户] 我们讨论一下数据库迁移，从 MySQL 换到 PostgreSQL，因为需要 JSONB 支持。
[助手] 合理，JSONB 的查询性能和灵活性都更好。
[用户] 决定下个 sprint 先跑试点。

输出:
{{
  "summaries": [
    {{
      "topic": "数据库迁移",
      "content": "讨论了从 MySQL 迁移到 PostgreSQL 的原因（JSONB 支持），决定下个 sprint 先跑试点。",
      "category": "technical_discussion",
      "confidence": 0.9
    }}
  ]
}}"""

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
        messages: Sequence[Message | dict[str, Any]] | None = None,
    ) -> str | None:
        """达到阈值时生成摘要并写入记忆."""
        if turn_count <= 0 or turn_count % self._summary_threshold != 0:
            return None

        conversation = self._format_messages(messages or [])
        if not conversation.strip():
            return None

        summaries = await self._summarize(conversation)
        if not summaries:
            return None

        saved_texts: list[str] = []
        for s in summaries:
            content = f"{s.topic}: {s.content}"
            try:
                await self._memory.add_memory(
                    content=content,
                    metadata={
                        "session_id": session_id,
                        "type": "summary",
                        "category": s.category,
                        "confidence": s.confidence,
                        "turn_count": turn_count,
                        "source": "threshold",
                    },
                    pinned=False,
                )
                saved_texts.append(content)
            except Exception as e:
                logger.warning("summary_save_failed", error=str(e))

        if saved_texts:
            logger.info(
                "conversation_summary_saved",
                session_id=session_id,
                turn_count=turn_count,
                count=len(saved_texts),
            )
        return "\n".join(saved_texts) if saved_texts else None

    async def _summarize(self, conversation: str) -> list[Summary]:
        """调用 LLM 生成结构化摘要，提示词已声明纯 JSON 返回格式，直接解析."""
        prompt = self.SUMMARIES_PROMPT.format(conversation=conversation)
        try:
            import json
            import re

            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = extract_message_text(response)
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                return []
            data = json.loads(match.group(0))
            return SummaryList.model_validate(data).summaries
        except Exception as e:
            logger.warning("summary_generation_failed", error=str(e))
            return []

    def _format_messages(self, messages: Sequence[Message | dict[str, Any]]) -> str:
        if not messages:
            return ""
        parts: list[str] = []
        for m in messages:
            if isinstance(m, Message):
                role = m.role.value
                content = m.content
            else:
                role = m.get("role", "")
                content = m.get("content", "")
            if role and content:
                parts.append(f"{role}: {content}")
        return "\n".join(parts)
