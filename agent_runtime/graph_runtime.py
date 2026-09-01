"""基于 LangGraph 的 Agent 运行时与子 Agent 管理。

本模块是 Athena 系统的核心编排层，负责将用户消息路由到合适的执行路径，
并协调记忆注入、上下文压缩、事实提取、阈值摘要等子系统。

核心组件：
- ``LangGraphRuntime``  — 为 LangGraph 节点提供领域服务和运行时能力。
- ``SubAgentManager`` — 子 Agent 生命周期管理器，支持串行 / 并行子任务派生，
  与父级共享工具集与审批队列。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessRunResult, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.core.memory.summarizer import ConversationSummarizer, FactExtractor
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from athena.models import Message, MessageRole
from athena.models.file import (
    Attachment,
    AttachmentRef,
    AttachmentStatus,
)
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import EventPublisherPort
from athena.utils.ids import generate_sub_run_id, generate_time_id
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

from .state import AgentState

logger = get_logger(__name__)

# ── 默认系统提示词 — 从 prompt/system.md 加载 ──
# 工具定义（含审批/风险治理信息）统一由 bind_tools 的函数 schema 注入，
# 提示词内不再重复罗列工具列表。

DEFAULT_SYSTEM_PROMPT = get_prompt("system")


class SubAgentResult(BaseModel):
    """子 Agent 执行结果.

    属性：
        task: 分配给子 Agent 的任务描述。
        content: 子 Agent 返回的文本内容；执行失败时为空字符串。
        turn_count: 子 Agent 与 LLM 的交互轮次。
        tool_results: 子 Agent 调用工具的返回结果列表。
        error: 错误信息；成功时为 ``None``。
        run_id: 子 Agent 的运行 ID，格式为 ``"{parent_run_id}_{index}"``。
    """

    task: str
    content: str
    turn_count: int = 0
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    run_id: str | None = None


class SubAgentManager:
    """子 Agent 生命周期管理器。

    通过依赖注入共享父级的 LLM、工具集、数据库连接和审批队列，
    使子 Agent 能够无缝访问父级资源，同时保持独立的运行上下文。

    设计约束：
        子 Agent 共享父 ``UnifiedToolManager`` 实例，通过 ``allowed_tools``
        白名单过滤可用工具；共享同一 ``ContextCompressor`` 实例以复用
        压缩摘要缓冲区。

    参数：
        llm: LLM 提供者实例。
        tool_manager: 统一工具管理器（与父级共享）。
        db: 数据库连接。
        event_publisher: 应用事件发布器，用于发布子 Agent 生命周期事件。
        compressor: 上下文压缩器（与父级共享摘要缓冲区）。
        memory_manager: 长期记忆管理器。
        settings: 全局配置。
        main_run_id: 父级运行 ID，用于生成子 Agent 的 ``sub_run_id``。
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        db: Database,
        event_publisher: EventPublisherPort,
        compressor: ContextCompressor,
        memory_manager: MemoryManager,
        settings: Settings,
        main_run_id: str,
    ) -> None:
        """

        参数：
            llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            tool_manager (UnifiedToolManager): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            db (数据库): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            event_publisher: 应用事件发布器。
            compressor (ContextCompressor): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            memory_manager (MemoryManager): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            settings (Settings): 全局配置对象。
            main_run_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._events = event_publisher
        self._compressor = compressor
        self._memory_manager = memory_manager
        self._settings = settings
        self._main_run_id = main_run_id
        self._sub_counter = 0
        self._lock = asyncio.Lock()
        self._parallel_semaphore = asyncio.Semaphore(6)

    async def spawn(
        self,
        task: str,
        session_id: str,
        allowed_tools: list[str] | None = None,
        max_turns: int = 5,
        stop_signal: asyncio.Event | None = None,
    ) -> SubAgentResult:
        """创建并执行一个子 Agent。

        子 Agent 在独立的 Harness 中运行，拥有自己的 LLM 交互轮次上限，
        但共享父级的工具集和审批队列。执行完成后发布应用事件
        生命周期事件（启动 / 完成 / 失败）。

        参数：
            task: 子任务的自然语言描述，直接作为子 Agent 的首轮用户消息。
            session_id: 当前会话 ID，用于事件推送和消息持久化。
            allowed_tools: 工具白名单；``None`` 表示不限制，继承父级全部工具。
            max_turns: 子 Agent 最大 LLM 交互轮次，默认 5。
            stop_signal: 停止事件，置位时终止子 Agent 执行。

        返回值：
            ``SubAgentResult``：包含子 Agent 输出内容、轮次数和工具调用结果。
            执行失败时 ``error`` 字段非空，``content`` 为空字符串。
        """
        async with self._lock:
            self._sub_counter += 1
            index = self._sub_counter
        sub_run_id = (
            generate_sub_run_id(self._main_run_id, index)
            if self._main_run_id
            else generate_time_id()
        )

        # 推送子 Agent 启动事件
        await self._events.publish(
            ApplicationEvent(
                event_type=str(EventType.SUB_AGENT_SPAWNED),
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=sub_run_id,
                payload={
                    "task": task,
                    "sub_run_id": sub_run_id,
                    "max_turns": max_turns,
                },
            )
        )

        # 子 Agent 使用过滤后的工具集（共享同一 manager，但通过 tool_names 白名单
        # 过滤 bind_tools 绑定与执行路径，allowed_tools=None 表示不限制）
        sub_harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            event_publisher=self._events,
            compressor=self._compressor,
            harness_settings=HarnessSettings(max_turns_per_run=max_turns),
        )

        try:
            result = await sub_harness.run(
                messages=[{"role": "user", "content": task}],
                session_id=session_id,
                system_prompt=get_prompt("sub_agent"),
                run_id=sub_run_id,
                parent_run_id=self._main_run_id,
                tool_names=allowed_tools,
                stop_signal=stop_signal,
            )
            sub_result = SubAgentResult(
                task=task,
                content=result.content,
                turn_count=result.turn_count,
                tool_results=result.tool_results,
                error=result.error,
                run_id=sub_run_id,
            )
            await self._events.publish(
                ApplicationEvent(
                    event_type=str(EventType.SUB_AGENT_COMPLETE),
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=sub_run_id,
                    payload={
                        "task": task,
                        "sub_run_id": sub_run_id,
                        "turn_count": result.turn_count,
                    },
                )
            )
            return sub_result
        except Exception as e:
            logger.exception("sub_agent_failed", sub_run_id=sub_run_id)
            await self._events.publish(
                ApplicationEvent(
                    event_type=str(EventType.SUB_AGENT_FAILED),
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=sub_run_id,
                    payload={"task": task, "sub_run_id": sub_run_id, "error": str(e)},
                )
            )
            return SubAgentResult(
                task=task, content="", error=str(e), run_id=sub_run_id
            )

    async def parallel(
        self,
        tasks: list[str],
        session_id: str,
        allowed_tools: list[str] | None = None,
        max_turns: int = 5,
        stop_signal: asyncio.Event | None = None,
    ) -> list[SubAgentResult]:
        """并行派生并执行多个子 Agent。

        使用 ``asyncio.gather`` 并发调度所有子任务，受 ``_parallel_semaphore``
        限制最大并发数。单个子 Agent 的异常不影响其余子 Agent 的执行。

        参数：
            tasks: 子任务描述列表，每个元素对应一个子 Agent。
            session_id: 当前会话 ID。
            allowed_tools: 工具白名单，所有子 Agent 共享。
            max_turns: 每个子 Agent 的最大 LLM 交互轮次。
            stop_signal: 停止事件，置位时终止所有子 Agent。

        返回值：
            执行结果列表，长度 <= ``len(tasks)``。失败的子 Agent 结果
            以日志形式记录，不包含在返回值中。
        """

        async def _spawn_with_semaphore(task: str) -> SubAgentResult:
            """

            参数：
                task (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

            返回值：
                SubAgentResult: 返回该方法声明类型的业务结果，内容由方法职责确定。

            异常：
                异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
            """
            async with self._parallel_semaphore:
                return await self.spawn(
                    task,
                    session_id,
                    allowed_tools,
                    max_turns=max_turns,
                    stop_signal=stop_signal,
                )

        results = await asyncio.gather(
            *[_spawn_with_semaphore(t) for t in tasks],
            return_exceptions=True,
        )
        out: list[SubAgentResult] = []
        for r in results:
            if isinstance(r, SubAgentResult):
                out.append(r)
            else:
                logger.error("sub_agent_exception", error=str(r))
        return out


class LangGraphRuntime:
    """为 LangGraph 提供领域执行能力的运行时服务。

    负责将用户消息路由到 Harness 执行引擎，并协调以下子系统：

    - **记忆检索**：从长期记忆中召回与当前消息相关的上下文，注入系统提示。
    - **上下文压缩**：通过增量摘要控制 LLM 的上下文窗口大小。
    - **事实提取**：异步从用户消息中提取原子事实，写入长期记忆。
    - **阈值摘要**：每 N 轮对话触发一次摘要，丰富长期记忆中的对话概览。
    - **子 Agent 派生**：将可并行的子任务委托给独立的子 Agent 执行。

    生命周期：
        由 LangGraph 节点按请求调用本服务；本对象不直接接收用户命令。
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        db: Database,
        event_publisher: EventPublisherPort,
        compressor: ContextCompressor,
        memory_retrieval: MemoryRetrievalService,
        conversation_summarizer: ConversationSummarizer,
        fact_extractor: FactExtractor,
        memory_manager: MemoryManager,
        settings: Settings,
        file_runtime: Any | None = None,
    ) -> None:
        """

        参数：
            llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            tool_manager (UnifiedToolManager): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            db (数据库): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            event_publisher: 应用事件发布器。
            compressor (ContextCompressor): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            memory_retrieval (MemoryRetrievalService): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            conversation_summarizer (ConversationSummarizer): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            fact_extractor (FactExtractor): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            memory_manager (MemoryManager): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            settings (Settings): 全局配置对象。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._events = event_publisher
        self._compressor = compressor
        self._memory_retrieval = memory_retrieval
        self._conversation_summarizer = conversation_summarizer
        self._fact_extractor = fact_extractor
        self._memory_manager = memory_manager
        self._settings = settings
        self._file_runtime = file_runtime
        self._file_parse_semaphore = asyncio.Semaphore(
            max(1, settings.file_parse_concurrency)
        )
        self._file_embedding_semaphore = asyncio.Semaphore(
            max(1, settings.file_embedding_concurrency)
        )
        # 会话级停止事件，由 gateway 层注入，透传给 Harness 及子 Agent。
        self._session_stop_signals: dict[str, asyncio.Event | None] = {}

    def set_stop_signal(
        self, session_id: str, stop_signal: asyncio.Event | None
    ) -> None:
        """设置会话对应的协作式停止信号。

        参数:
            session_id (str): 会话 ID。
            stop_signal (asyncio.Event | None): 停止事件；``None`` 表示清除当前信号。
        返回值:
            None: 后续 Harness 执行将读取该信号。
        异常:
            不抛出业务异常。
        """
        self._session_stop_signals[session_id] = stop_signal

    def delegation_tool_specs(self):
        """构建供组合根注册的 Agent 委派工具声明。

        返回值:
            list[ToolSpec]: 子 Agent 串行和并行派生工具声明。
        异常:
            工具声明构建失败时传播底层异常。
        """
        from athena.core.tools.providers.agents import build_agent_tool_specs

        return build_agent_tool_specs(
            self._spawn_sub_agent_handler,
            self._spawn_parallel_handler,
            self._build_parallel_spawn_description(),
        )

    async def load_banded_attachments(
        self,
        session_id: str,
        attachment_ids: list[str],
    ) -> list[Attachment]:
        """加载并校验请求附件是否属于当前会话。

        参数：
            session_id (str): 会话唯一标识。
            attachment_ids (list[str]): 待加载的附件 ID 列表。

        返回值：
            list[Attachment]: 按请求顺序返回属于当前会话的附件。

        异常：
            ValueError: 附件不存在、不属于当前会话，或首次提交时附件处理已失败。
            其他异常: 数据库读取失败时向上传播。
        """
        if not attachment_ids:
            return []
        attachments = await self._db.files.get_attachments(session_id, attachment_ids)
        attachments_by_id = {item.id: item for item in attachments}
        result: list[Attachment] = []
        for file_id in attachment_ids:
            attachment = attachments_by_id.get(file_id)
            if attachment is None:
                raise ValueError("附件不存在或不属于当前会话")
            if attachment.status == AttachmentStatus.FAILED:
                raise ValueError(f"附件 {attachment.filename} 处理失败，不能随消息提交")
            result.append(attachment)
        return result

    async def retrieve_memory_context(self, session_id: str, user_message: str) -> str:
        """检索与用户消息相关的长期记忆上下文。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 用于检索的用户消息。

        返回值：
            str: 格式化后的记忆上下文；检索失败时返回空字符串。

        异常：
            不向主流程传播检索异常；失败仅记录警告并返回空字符串。
        """
        try:
            context = await self._memory_retrieval.get_relevant_memories(
                user_message=user_message
            )
            logger.info(
                "memory_injection_success",
                session_id=session_id,
                memory_context_length=len(context),
            )
            return context
        except Exception as e:
            logger.warning("memory_injection_failed", error=str(e))
            return ""

    async def load_history(self, session_id: str) -> list[Message]:
        """加载会话历史，并在存在压缩摘要时拼接增量消息。

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[Message]: 可供 Harness 使用的历史消息列表。

        异常：
            数据库读取失败时向上传播底层异常。
        """
        session = await self._db.sessions.get(session_id)
        if not session:
            raise ValueError(f"会话 {session_id} 不存在")
        if not (session.compression_summary and session.last_compressed_message_id):
            return await self._db.messages.get_by_session(session_id)

        history_after = await self._db.messages.get_after_message(
            session_id, session.last_compressed_message_id
        )
        summary = Message(
            id=generate_time_id(),
            session_id=session_id,
            role=MessageRole.SYSTEM,
            content=f"[对话历史摘要]\n{session.compression_summary}",
            type="conversation_summary",
            timestamp=datetime.now(),
        )
        logger.info(
            "history_loaded_with_summary",
            session_id=session_id,
            incremental_count=len(history_after),
        )
        return [summary, *history_after]

    async def persist_message_and_attachments(self, state: AgentState) -> AgentState:
        """幂等持久化当前用户消息及其附件关系。"""
        message_id = state.get("message_id")
        message = Message(
            id=message_id,
            session_id=state["session_id"],
            role=MessageRole.USER,
            content=state.get("user_message", ""),
            run_id=state["run_id"],
            timestamp=datetime.now(),
        )
        persisted = await self._db.messages.create_message_with_attachments(
            message, state.get("attachment_ids", [])
        )
        await self._events.publish(
            ApplicationEvent(
                event_type="message.persisted",
                durability=EventDurability.DURABLE,
                session_id=state["session_id"],
                run_id=state["run_id"],
                message_id=persisted.id,
                payload={
                    "message_id": persisted.id,
                    "attachment_ids": state.get("attachment_ids", []),
                },
            )
        )
        return {
            "message_id": persisted.id,
            "user_message_id": persisted.id,
        }

    async def process_attachment(
        self, attachment_id: str, message_id: str, session_id: str, run_id: str
    ) -> dict[str, Any]:
        """在统一 Graph 内完成一个附件的解析和向量索引。"""
        if self._file_runtime is None:
            raise RuntimeError("file runtime is not configured")
        await self._events.publish(
            ApplicationEvent(
                event_type="file_processing_started",
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                message_id=message_id,
                attachment_id=attachment_id,
                payload={"message_id": message_id, "attachment_id": attachment_id},
            )
        )
        try:
            async with self._file_parse_semaphore:
                metadata = await self._file_runtime.parse_attachment(
                    attachment_id, run_id=run_id
                )
            await self._file_runtime.emit(
                "file_processing_progress",
                session_id,
                {
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "run_id": run_id,
                    "progress": 0.7,
                },
            )
            async with self._file_embedding_semaphore:
                indexed = await self._file_runtime.index_attachment(
                    attachment_id, run_id=run_id
                )
            await self._file_runtime.emit(
                "file_index_progress",
                session_id,
                {
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "run_id": run_id,
                    "progress": 1.0,
                },
            )
            await self._file_runtime.emit(
                "file_processing_completed",
                session_id,
                {
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "run_id": run_id,
                    "chunk_count": int(
                        metadata.get("chunk_count", indexed.get("chunks", 0))
                    ),
                },
            )
            return {
                "message_id": message_id,
                "attachment_id": attachment_id,
                "status": "ready",
                "error": None,
                "chunk_count": int(
                    metadata.get("chunk_count", indexed.get("chunks", 0))
                ),
            }
        except Exception as exc:
            attachment = await self._db.files.update_attachment(
                attachment_id,
                status=AttachmentStatus.FAILED.value,
                error_message=str(exc),
            )
            if attachment is not None:
                await self._file_runtime.emit_attachment(attachment, run_id=run_id)
                session_id = attachment.session_id
            await self._file_runtime.emit(
                "file_processing_failed",
                session_id,
                {
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "run_id": run_id,
                    "error": str(exc),
                },
            )
            return {
                "message_id": message_id,
                "attachment_id": attachment_id,
                "status": "failed",
                "error": str(exc),
                "chunk_count": None,
            }

    @staticmethod
    def build_system_prompt(memory_context: str) -> str:
        """使用内置系统提示词，并在存在记忆时追加记忆上下文。

        参数：
            memory_context (str): 检索得到的记忆上下文；为空时不追加换行。

        返回值：
            str: 最终供 Harness 使用的系统提示词。

        异常：
            不抛出业务异常。
        """
        return (
            (DEFAULT_SYSTEM_PROMPT + "\n\n" + memory_context).strip()
            if memory_context
            else DEFAULT_SYSTEM_PROMPT
        )

    async def prepare_run(
        self,
        session_id: str,
        user_message: str,
        attachment_ids: list[str],
        history: list[Message],
        run_id_override: str | None = None,
        message_id_override: str | None = None,
    ) -> tuple[Message, list[AttachmentRef], list[Message]]:
        """

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            attachment_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            history (list[Message]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            tuple[Message, list[AttachmentRef], list[Message]]: 依次返回用户消息、附件引用和 Harness 消息。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        run_id = run_id_override or generate_time_id()
        message = await self._get_or_create_user_message(
            session_id,
            user_message,
            run_id,
            message_id_override,
        )
        attachment_refs = await self._bind_message_attachments(
            session_id, message, attachment_ids
        )
        messages = self._build_harness_messages(history, message)
        return message, attachment_refs, messages

    async def _get_or_create_user_message(
        self,
        session_id: str,
        content: str,
        run_id: str,
        message_id_override: str | None = None,
    ) -> Message:
        """

        参数：
            session_id (str): 会话唯一标识。
            content (str): 待保存或处理的内容。
            run_id (str): 运行唯一标识。

        返回值：
            Message: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        message_id = message_id_override
        if message_id:
            existing = await self._db.messages.get(str(message_id))
            if existing is not None:
                return existing
            return Message(
                id=str(message_id),
                session_id=session_id,
                role=MessageRole.USER,
                content=content,
                run_id=run_id,
                timestamp=datetime.now(),
            )

        message = Message(
            id=generate_time_id(),
            session_id=session_id,
            role=MessageRole.USER,
            content=content,
            run_id=run_id,
            timestamp=datetime.now(),
        )
        await self._db.messages.save(message)
        return message

    async def _bind_message_attachments(
        self,
        session_id: str,
        message: Message,
        attachment_ids: list[str],
    ) -> list[AttachmentRef]:
        """

        参数：
            session_id (str): 会话唯一标识。
            message (Message): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            attachment_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[AttachmentRef]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not attachment_ids:
            return []
        attachments = await self._db.files.bind_message(
            session_id, message.id, attachment_ids
        )
        refs = [item.to_ref() for item in attachments]
        message.attachments = refs
        return refs

    @staticmethod
    def _build_harness_messages(
        history: list[Message],
        user_message: Message,
    ) -> list[Message]:
        """

        参数：
            history (list[Message]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            user_message (Message): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[Message]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        base_messages = [*history]
        if not any(item.id == user_message.id for item in base_messages):
            base_messages.append(user_message)

        messages: list[Message] = []
        for message in base_messages:
            if message.role != MessageRole.USER or not message.attachments:
                messages.append(message)
                continue
            refs = "\n".join(
                f"- file_id={ref.id}; name={ref.filename}; status={ref.status.value}; "
                f"mime={ref.mime_type}; size={ref.size_bytes}"
                for ref in message.attachments
            )
            context = (
                "\n\n[该用户消息关联的文件资产]\n"
                f"{refs}\n文件正文不会自动注入上下文。"
                "需要内容时，必须使用正式文件能力工具并传入上述 file_id。"
            )
            messages.append(
                message.model_copy(update={"content": message.content + context})
            )
        return messages

    async def run_harness(
        self,
        messages: list[Message],
        session_id: str,
        memory_context: str,
        run_id: str,
        stop_signal: asyncio.Event | None,
    ) -> HarnessRunResult:
        """

        参数：
            messages (list[Message]): 已构建的 Harness 消息列表。
            session_id (str): 会话唯一标识。
            memory_context (str): 检索得到的记忆上下文。
            run_id (str): 当前运行 ID。
            stop_signal (asyncio.Event | None): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            HarnessRunResult: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            event_publisher=self._events,
            compressor=self._compressor,
        )
        result = await harness.run(
            messages=messages,
            session_id=session_id,
            system_prompt=self.build_system_prompt(memory_context),
            run_id=run_id,
            stop_signal=stop_signal,
        )
        logger.info(
            "task_classification_result",
            session_id=session_id,
            turn_count=result.turn_count,
            tool_count=len(result.tool_results),
            had_error=bool(result.error),
            interrupted=result.interrupted,
        )
        return result

    async def post_process(
        self, session_id: str, user_message: str, result: HarnessRunResult
    ) -> None:
        """

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            result (HarnessRunResult): 方法返回的领域结果。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        asyncio.create_task(
            self._extract_facts_async(user_message, result.content, session_id)
        )
        try:
            await self._conversation_summarizer.summarize_if_needed(
                session_id=session_id, db=self._db
            )
        except Exception as e:
            logger.warning("summary_trigger_failed", error=str(e))

    @classmethod
    def result_payload(
        cls, result: HarnessRunResult, attachment_refs: list[AttachmentRef]
    ) -> dict[str, Any]:
        """

        参数：
            result (HarnessRunResult): 方法返回的领域结果。
            attachment_refs (list[AttachmentRef]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return {
            "content": result.content,
            "run_id": result.run_id,
            "turn_count": result.turn_count,
            "tool_results": result.tool_results,
            "error": result.error,
            "interrupted": result.interrupted,
            "attachments": cls._serialize_attachments(attachment_refs),
        }

    @staticmethod
    def deserialize_attachment_refs(
        payload: list[dict[str, Any]],
    ) -> list[AttachmentRef]:
        """从 LangGraph 状态恢复附件轻量引用。"""
        return [AttachmentRef.model_validate(item) for item in payload]

    @staticmethod
    def deserialize_messages(payload: list[dict[str, Any]]) -> list[Message]:
        """从 LangGraph 状态恢复 Harness 消息。"""
        return [Message.model_validate(item) for item in payload]

    @staticmethod
    def _serialize_attachments(refs: list[AttachmentRef]) -> list[dict[str, Any]]:
        """

        参数：
            refs (list[AttachmentRef]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[dict[str, Any]]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return [item.model_dump(mode="json") for item in refs]

    async def _extract_facts_async(
        self,
        user_message: str,
        assistant_reply: str,
        session_id: str,
    ) -> None:
        """异步提取对话中的原子事实并写入长期记忆。

        将用户消息与助手回复组合为对话文本进行提取，以捕获决策闭环
        （如用户采纳助手建议的方案）。``user_message`` 单独用于记忆检索
        的语义查询，确保召回与用户意图相关的已有记忆。

        作为 ``asyncio.create_task`` 的目标运行，不阻塞主编排流程。
        提取失败时仅记录警告日志，不影响主流程返回结果。

        参数：
            user_message: 用户消息的原始文本。
            assistant_reply: 助手回复的文本内容；为空时退化为仅提取用户消息。
            session_id: 当前会话 ID。
        """
        try:
            await self._fact_extractor.extract(
                user_message,
                assistant_reply,
                session_id,
                self._memory_manager,
            )
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))

    @staticmethod
    def _build_parallel_spawn_description() -> str:
        """构建 ``spawn_parallel_agents`` 工具描述。

        指导 LLM 在何时使用并行子 Agent 而非串行子 Agent。
        """
        return (
            "Spawn multiple independent sub-agents that run concurrently. "
            "Use this when a task can be decomposed into independent subtasks "
            "that have no data dependencies between them and each can be "
            "completed in isolation. This reduces total execution time by "
            "running them in parallel.\n\n"
            "Do NOT use when:\n"
            "- Subtasks depend on each other's results\n"
            "- The task requires sequential reasoning\n"
            "- Only one subtask is needed (use spawn_sub_agent instead)\n\n"
            "Each sub-agent runs independently with its own LLM context. "
            "Results are returned as a JSON array after ALL agents complete."
        )

    async def _spawn_parallel_handler(
        self,
        tasks: list[str],
        session_id: str,
        parent_run_id: str,
        max_turns: int = 5,
    ) -> str:
        """``spawn_parallel_agents`` 工具的执行处理器。

        并行派生多个子 Agent 执行独立子任务，汇总结果后返回 JSON 数组。

        参数：
            tasks: 子任务描述列表（2-6 个）。
            session_id: 当前会话 ID。
            parent_run_id: 父级运行 ID，由工具管理器从调用链注入。
            max_turns: 每个子 Agent 的最大 LLM 交互轮次。

        返回值：
            JSON 数组字符串，每个元素包含 task/status/output/error/turns_used；
            输入校验失败时返回错误信息 JSON。
        """
        if len(tasks) < 2:
            return json.dumps(
                {"error": "Need at least 2 tasks for parallel execution"},
                ensure_ascii=False,
            )
        if len(tasks) > 6:
            return json.dumps(
                {"error": "Maximum 6 parallel tasks allowed"},
                ensure_ascii=False,
            )

        logger.info(
            "parallel_agents_invoked",
            session_id=session_id,
            parent_run_id=parent_run_id,
            task_count=len(tasks),
        )

        # 推送并行启动事件
        await self._events.publish(
            ApplicationEvent(
                event_type=str(EventType.PARALLEL_AGENTS_STARTED),
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=parent_run_id,
                payload={"task_count": len(tasks), "tasks": [t[:500] for t in tasks]},
            )
        )

        try:
            manager = self.get_sub_agent_manager(main_run_id=parent_run_id)
            stop_signal = self._session_stop_signals.get(session_id)
            results = await manager.parallel(
                tasks=tasks,
                session_id=session_id,
                max_turns=max_turns,
                stop_signal=stop_signal,
            )

            output = [
                {
                    "task": r.task,
                    "status": "success" if not r.error else "error",
                    "output": r.content[:2000] if r.content else "",
                    "error": r.error,
                    "turns_used": r.turn_count,
                }
                for r in results
            ]

            success_count = sum(1 for r in results if not r.error)
            logger.info(
                "parallel_agents_completed",
                session_id=session_id,
                total=len(tasks),
                success=success_count,
                failed=len(tasks) - success_count,
            )
            return json.dumps(output, ensure_ascii=False)
        except Exception as e:
            logger.exception("parallel_agents_exception", session_id=session_id)
            return json.dumps(
                {"error": f"[PARALLEL AGENTS ERROR] {e}"},
                ensure_ascii=False,
            )

    async def _spawn_sub_agent_handler(
        self, task: str, session_id: str, parent_run_id: str
    ) -> str:
        """``spawn_sub_agent`` 工具的执行处理器。

        作为注册到 ``UnifiedToolManager`` 的 native handler 被 Harness 调用。
        创建子 Agent 执行独立子任务，返回其输出结果。失败时返回错误信息
        而非抛出异常，使 LLM 能够感知失败并决定是否重试或降级处理。

        参数：
            task: 子任务的自然语言描述。
            session_id: 当前会话 ID。
            parent_run_id: 父级运行 ID，由工具管理器从调用链注入
                （Harness 执行工具时携带自身 run_id）。子 Agent 据此生成
                带父链的 sub_run_id（``"{parent_run_id}_{index}"``），
                前端可将子任务的步骤归组到父请求下。

        返回值：
            子 Agent 的输出文本；失败时返回 ``"[SUB-AGENT ERROR] {error}"``；
            无输出时返回 ``"[SUB-AGENT] Completed with no output."``。
        """
        logger.info(
            "sub_agent_tool_invoked",
            session_id=session_id,
            parent_run_id=parent_run_id,
            task=task[:200],
        )
        try:
            manager = self.get_sub_agent_manager(main_run_id=parent_run_id)
            stop_signal = self._session_stop_signals.get(session_id)
            result = await manager.spawn(
                task=task, session_id=session_id, stop_signal=stop_signal
            )
            if result.error:
                logger.warning(
                    "sub_agent_tool_error",
                    session_id=session_id,
                    error=result.error,
                )
                return f"[SUB-AGENT ERROR] {result.error}"
            logger.info(
                "sub_agent_tool_success",
                session_id=session_id,
                turn_count=result.turn_count,
            )
            return result.content or "[SUB-AGENT] Completed with no output."
        except Exception as e:
            logger.exception("sub_agent_tool_exception", session_id=session_id)
            return f"[SUB-AGENT ERROR] {e}"

    def get_sub_agent_manager(self, main_run_id: str) -> SubAgentManager:
        """创建子 Agent 管理器实例。

        每次调用返回新实例，共享当前工作流的 LLM、工具集、数据库连接
        和压缩器等依赖。``main_run_id`` 用于生成子 Agent 的 ``sub_run_id``。

        参数：
            main_run_id: 父级运行 ID；为 ``None`` 时子 Agent 使用独立的时间戳 ID。

        返回值：
            配置完成的 ``SubAgentManager`` 实例。
        """
        return SubAgentManager(
            llm=self._llm,
            tool_manager=self._tool_manager,
            db=self._db,
            event_publisher=self._events,
            compressor=self._compressor,
            memory_manager=self._memory_manager,
            settings=self._settings,
            main_run_id=main_run_id,
        )
