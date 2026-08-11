"""记忆摘要与事实提取。

本模块提供两个互补的记忆沉淀组件，将对话中的高价值信息持久化到长期记忆：

- ``FactExtractor`` — 从对话中提取原子事实（key/value 结构）。
  支持用户消息 + 助手回复的多轮对话输入，具备跨会话语义去重能力。
- ``ConversationSummarizer`` — 每 N 轮对话触发一次摘要生成，
  将讨论主线、决策过程和问题解决路径提炼为结构化摘要条目。

两者均通过 ``MemoryManager.add_memory()`` 写入长期记忆，供下游的
``MemoryRetrievalService`` 在后续对话中召回注入系统提示。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from athena.config.settings import Settings, get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.db.database import Database
from athena.models import Message, MessageRole
from athena.utils.llm import extract_json_from_llm_response, extract_message_text
from athena.utils.message import format_messages_brief
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

logger = get_logger(__name__)


class AtomicFact(BaseModel):
    """原子事实。

    表示从对话中提取的一条独立、自包含、可验证的信息单元，
    以 key/value 结构存储，附带分类和置信度元数据。

    Attributes:
        key: 简短标识符，snake_case 格式，≤30 字符。
        value: 事实内容，需自包含且可独立理解，≤100 字符。
        category: 事实分类，取值范围见 ``CATEGORIES`` 常量说明。
        confidence: 置信度 0-1，低于 0.6 的事实在提取时被过滤。
    """

    CATEGORIES: ClassVar[str] = (
        "preference | profile | project | technical_decision | "
        "fact | solution | unresolved | other"
    )

    key: str = Field(description="简短标识符，snake_case，≤30 字符")
    value: str = Field(description="事实内容，自包含，≤100 字符")
    category: str = Field(
        default="fact",
        description=f"分类: {CATEGORIES}",
    )
    confidence: float = Field(
        default=0.8, ge=0.0, le=1.0, description="置信度 0-1，低于 0.6 不输出"
    )


class AtomicFactList(BaseModel):
    """原子事实列表的 LLM 结构化输出包装器。

    用于 ``FactExtractor._extract_facts()`` 的 JSON 解析目标结构。
    LLM 返回的 JSON 经 ``model_validate()`` 校验后提取 ``atomic_facts`` 字段。
    """

    atomic_facts: list[AtomicFact] = Field(default_factory=list)


class FactExtractor:
    """从对话中提取原子事实并写入长期记忆。

    提取流程：
    1. 以用户消息为查询，从 ``MemoryManager`` 召回相关已有记忆（语义去重）。
    2. 将用户消息 + 助手回复格式化为多轮对话文本。
    3. 调用 LLM 按提取规则生成结构化 ``AtomicFact`` 列表。
    4. 逐条写入 ``MemoryManager``，附带 session_id / category / confidence 元数据。

    设计约束：
        使用 ``ainvoke`` 而非 ``with_structured_output``，以兼容不支持
        结构化输出的 Provider（如 Ollama/DeepSeek），且 ``ainvoke`` 经过
        重试管理器，容错性更强。
    """

    EXTRACTION_PROMPT = get_prompt("fact_extraction")

    def __init__(self, llm_provider: LLMProvider) -> None:
        """初始化事实提取器。

        Args:
            llm_provider: LLM 提供者实例，用于调用提取提示词。
        """
        self._llm = llm_provider

    async def extract(
        self,
        user_message: str,
        assistant_reply: str,
        session_id: str,
        memory_manager: MemoryManager,
    ) -> list[AtomicFact]:
        """从对话中提取原子事实并写入长期记忆。

        将用户消息与助手回复格式化为多轮对话文本送入 LLM 提取，
        以捕获决策闭环（如用户采纳助手方案）。使用 ``user_message``
        单独进行记忆检索的语义查询，确保召回与用户意图相关的已有记忆。

        Args:
            user_message: 用户消息原始文本，用于记忆检索查询。
            assistant_reply: 助手回复文本；为空时退化为仅提取用户消息。
            session_id: 当前会话 ID。
            memory_manager: 长期记忆管理器实例。

        Returns:
            提取到的原子事实列表；提取失败或无有价值信息时为空列表。
        """
        existing = await self._fetch_existing_memories(
            memory_manager, user_message,
        )
        conversation_text = self._format_conversation(user_message, assistant_reply)
        facts = await self._extract_facts(conversation_text, existing)
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

    @staticmethod
    def _format_conversation(user_message: str, assistant_reply: str) -> str:
        """将用户消息与助手回复格式化为多轮对话文本。

        格式与 ``ConversationSummarizer._format_messages`` 保持一致，
        便于 LLM 统一理解对话结构。

        Args:
            user_message: 用户消息原始文本。
            assistant_reply: 助手回复文本；为空时仅返回用户消息。

        Returns:
            格式化的对话文本，每行一个角色发言。
        """
        lines = [f"[用户] {user_message}"]
        if assistant_reply:
            lines.append(f"[助手] {assistant_reply}")
        return "\n".join(lines)

    async def _fetch_existing_memories(
        self,
        memory_manager: MemoryManager,
        message: str,
        limit: int = 8,
    ) -> str:
        """检索与当前消息语义相关的已有记忆，供提取提示词做去重。

        跨会话召回：与所有会话已建立的记忆做语义比较，避免同一事实
        被重复写入记忆库（历史上按当前会话过滤会导致跨会话重复）。

        Args:
            memory_manager: 长期记忆管理器实例。
            message: 用于语义检索的查询文本（用户消息）。
            limit: 最大召回条数，默认 8。

        Returns:
            格式化的已有记忆文本，每行一条（``"- content (category: xxx)"``）；
            检索失败或无结果时返回 ``"(无已有记忆)"``。
        """
        try:
            results = await memory_manager.search(query=message, n_results=limit)
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
        """调用 LLM 从对话文本中提取原子事实。

        提示词声明纯 JSON 返回格式，通过正则提取 JSON 块后用 Pydantic
        校验解析。使用 ``ainvoke`` 路径以兼容不支持结构化输出的 Provider。

        Args:
            conversation_text: 格式化的多轮对话文本（``[用户]`` / ``[助手]``）。
            existing_memories: 已有记忆的格式化文本，用于提示词语义去重。

        Returns:
            提取到的原子事实列表；LLM 返回无效 JSON 或无内容时为空列表。
        """
        prompt = self.EXTRACTION_PROMPT.format(
            existing_memories=existing_memories,
            conversation_text=conversation_text,
        )
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = extract_message_text(response)
            data = extract_json_from_llm_response(content)
            if data is None:
                return []
            return AtomicFactList.model_validate(data).atomic_facts
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))
            return []


class Summary(BaseModel):
    """对话摘要条目。

    表示一段对话的结构化摘要，对应一个讨论主线或问题解决过程，
    保留关键上下文、决策与理由。

    Attributes:
        topic: 讨论主题的简短标签。
        content: 摘要正文，2-3 句话，≤300 字符。
        category: 摘要分类，取值范围见 ``CATEGORIES`` 常量说明。
        confidence: 置信度 0-1，反映摘要内容的确定程度。
    """

    CATEGORIES: ClassVar[str] = (
        "technical_discussion | problem_solving | planning | "
        "decision | open_discussion | other"
    )

    topic: str = Field(description="讨论主题的简短标签")
    content: str = Field(description="摘要正文，2-3 句话，≤300 字符")
    category: str = Field(
        default="technical_discussion",
        description=f"分类: {CATEGORIES}",
    )
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class SummaryList(BaseModel):
    """摘要列表的 LLM 结构化输出包装器。

    用于 ``ConversationSummarizer._summarize()`` 的 JSON 解析目标结构。
    """

    summaries: list[Summary] = Field(default_factory=list)


class ConversationSummarizer:
    """对话摘要生成器。

    每隔 ``summary_threshold`` 轮对话触发一次摘要生成，将最近的对话内容
    提炼为结构化的 ``Summary`` 条目，通过 ``MemoryManager`` 写入长期记忆。

    与 ``FactExtractor`` 互补：``FactExtractor`` 提取离散的原子事实，
    ``ConversationSummarizer`` 提取连贯的叙事摘要，两者共同构成
    对话记忆的"点 + 线"沉淀体系。
    """

    SUMMARIES_PROMPT = get_prompt("conversation_summary")

    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_manager: MemoryManager,
        settings: Settings | None = None,
    ) -> None:
        """初始化对话摘要生成器。

        Args:
            llm_provider: LLM 提供者实例，用于调用摘要提示词。
            memory_manager: 长期记忆管理器实例，用于写入生成的摘要。
            settings: 全局配置；为 ``None`` 时使用默认配置。
        """
        self._llm = llm_provider
        self._memory = memory_manager
        self._settings = settings or get_settings()
        self._summary_threshold = self._settings.summary_threshold

    async def summarize_if_needed(
        self,
        session_id: str,
        db: Database,
    ) -> str | None:
        """增量触发摘要：按完整对话轮次为边界处理新消息。

        从 session 读取 ``last_summarized_message_id``，加载其后的增量消息，
        按用户消息边界分割为完整轮次，当完整轮次数达到 ``summary_threshold``
        时触发摘要。指针推进到最后一个完整轮次的末尾，不截断半轮对话。

        Args:
            session_id: 当前会话 ID。
            db: 数据库实例，用于加载增量消息和更新摘要指针。

        Returns:
            生成的摘要文本（多条以换行连接）；未触发或生成失败时返回 ``None``。
        """
        # ── 1. 加载增量消息 ──
        session_obj = await db.sessions.get(session_id)
        last_id = session_obj.last_summarized_message_id if session_obj else None

        if last_id:
            raw = await db.messages.get_after_message(session_id, last_id)
        else:
            raw = await db.messages.get_by_session(session_id)

        if not raw:
            return None

        # ── 2. 过滤真实对话消息（排除系统消息） ──
        conversation_messages = [
            m for m in raw
            if m.role in (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL)
        ]
        if not conversation_messages:
            return None

        # ── 3. 按用户消息边界分割为完整轮次 ──
        turns = self._split_turns(conversation_messages)

        # 末尾轮次若仅含一条用户消息（无后续助手回复），视为不完整，排除。
        if turns and turns[-1][0].role == MessageRole.USER and len(turns[-1]) == 1:
            turns = turns[:-1]

        if len(turns) < self._summary_threshold:
            return None

        # ── 4. 生成摘要并写入记忆 ──
        all_msgs = [m for turn in turns for m in turn]
        conversation = self._format_messages(all_msgs)
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
                        "source": "threshold",
                    },
                    pinned=False,
                )
                saved_texts.append(content)
            except Exception as e:
                logger.warning("summary_save_failed", error=str(e))

        # ── 5. 更新摘要进度指针 ──
        if saved_texts and all_msgs:
            last_msg_id = all_msgs[-1].id
            try:
                await db.sessions.update(
                    session_id,
                    last_summarized_message_id=last_msg_id,
                )
            except Exception as e:
                logger.warning("update_last_summarized_id_failed", error=str(e))

            logger.info(
                "conversation_summary_saved",
                session_id=session_id,
                count=len(saved_texts),
                turns_processed=len(turns),
            )

        return "\n".join(saved_texts) if saved_texts else None

    @staticmethod
    def _split_turns(messages: list[Message]) -> list[list[Message]]:
        """按用户消息边界分割为对话轮次。

        每个轮次从一条用户消息开始，到下一条用户消息之前结束。
        与 ``compression.pairer.MessagePairer`` 逻辑一致，但操作域模型。

        Args:
            messages: 已按时间排序的消息列表。

        Returns:
            轮次列表，每个元素是一轮对话的消息列表。
        """
        turns: list[list[Message]] = []
        current: list[Message] = []
        for msg in messages:
            if msg.role == MessageRole.USER and current:
                turns.append(current)
                current = []
            current.append(msg)
        if current:
            turns.append(current)
        return turns

    async def _summarize(self, conversation: str) -> list[Summary]:
        """调用 LLM 生成结构化对话摘要。

        提示词声明纯 JSON 返回格式，通过正则提取 JSON 块后用 Pydantic
        校验解析。使用 ``ainvoke`` 路径以兼容不支持结构化输出的 Provider。

        Args:
            conversation: 格式化的对话文本（``role: content`` 逐行格式）。

        Returns:
            摘要条目列表；LLM 返回无效 JSON 或无内容时为空列表。
        """
        prompt = self.SUMMARIES_PROMPT.format(conversation=conversation)
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = extract_message_text(response)
            data = extract_json_from_llm_response(content)
            if data is None:
                return []
            return SummaryList.model_validate(data).summaries
        except Exception as e:
            logger.warning("summary_generation_failed", error=str(e))
            return []

    @staticmethod
    def _format_messages(
        messages: Sequence[Message | dict[str, Any]],
    ) -> str:
        """将消息列表格式化为 ``role: content`` 逐行文本。"""
        return format_messages_brief(messages)
