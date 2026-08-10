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
from typing import Any

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

    CATEGORIES = (
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

    EXTRACTION_PROMPT = """# 角色
你是一个精确、保守的信息提取引擎。你的任务是从对话中提炼具有长期价值的信息，
并严格避免与已有记忆重复。

# 输入
- `existing_memories`: 之前提取的原子事实与摘要列表。任何与已有记忆语义等价的内容
  都不得再次提取。
- `conversation_text`: 待提取的对话内容（用户与助手消息，格式为 `[用户]` / `[助手]`）。

existing_memories:
{existing_memories}

conversation_text:
{conversation_text}

# 提取决策树
1. 如果对话仅包含问候、闲聊或琐碎交流 → 输出空 JSON（{{"atomic_facts": []}}）。
2. 如果对话未结束 → 只提取完全确认的事实（明确陈述且非假设），
   跳过未决决策或未成型的提议。
3. 否则 → 按下列规则正常提取。

# 提取规则

## A. 提取什么（长期价值）
只提取对未来交互可能有用的信息，包括：
- 用户偏好（如"偏好简洁回答"、"不喜欢 Python"）
- 个人画像（如"住在北京"、"是后端工程师"）
- 技术决策（如"选择 PostgreSQL 而非 MySQL"、"采用微服务架构"）
- 项目背景（如"仓库名: my-app"、"截止时间: 2026 Q3"）
- 问题解决方案（如"通过增大 keepalive 修复超时"）
- 需要跟进而未解决的问题（如"需要决定云服务商"）

不要提取：
- 一次性问答，之后不会再被引用
- 客套话或填充内容
- 临时状态（如"现在感觉很累"）

## B. 从助手回复中提取（决策闭环）
当用户明确采纳、确认或执行了助手的建议时，将被采纳的具体方案作为事实提取。
这捕获了"用户说好 → 具体是什么"的决策闭环。

判定标准（同时满足）：
1. 助手提出了具体方案、配置、命令或技术选型（非泛泛解释）。
2. 用户通过明确确认词采纳（如"好的"、"就用这个"、"按你说的改"、"就这样"）。

提取内容 = 助手方案的具体部分（如具体的工具名、配置值、命令），
而非助手的通用解释或背景知识。

不要从助手回复中提取：
- 通用技术解释（如"REST 是一种架构风格……"）
- 代码示例或教程性内容（除非用户明确采纳执行）
- 助手的推测或不确定的建议（用户未确认）

## C. 原子事实
每条原子事实是独立、自包含、可验证的信息。
示例：{{"key": "timezone", "value": "UTC+8", "category": "profile"}}。

## D. 避免重复（关键）
- 添加任何新条目前，先与所有 `existing_memories` 做语义比较。
- 若已有记忆存在（即使措辞略有不同），跳过该提取。
- 若新信息更新了已有记忆（如偏好从"Python"改为"Rust"），作为新事实提取，
  并在 value 中标注更新来源，如 "Rust (was Python)"。

## E. 置信度 (0-1)
按证据强度评分：
- 1.0: 用户明确清晰陈述
- 0.8-0.9: 由多次表述强推定
- 0.6-0.7: 从上下文推断但未明确确认
- < 0.6: 丢弃，不输出

从助手回复中提取的事实，置信度上限为 0.9（需用户确认才算完全确认）。

## F. 类别
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

## 示例 1：用户直接陈述
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
}}

## 示例 2：用户采纳助手方案（决策闭环）
输入:
conversation_text:
[用户] 端口被占用了怎么办？
[助手] 可以用 SO_REUSEADDR 选项，或者用 lsof -i :8080 找到占用进程后 kill 掉。
[用户] 用 lsof 那个方案吧，帮我查一下。

输出:
{{
  "atomic_facts": [
    {{"key": "port_conflict_solution", "value": "lsof -i :<port> 查找占用进程后 kill", "category": "solution", "confidence": 0.9}}
  ]
}}"""

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

    CATEGORIES = (
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
        session_obj = await db.get_session(session_id)
        last_id = session_obj.last_summarized_message_id if session_obj else None

        if last_id:
            raw = await db.get_messages_after(session_id, last_id)
        else:
            raw = await db.get_messages(session_id)

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
                await db.update_session(
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
