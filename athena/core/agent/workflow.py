"""Agent 工作流编排与子 Agent 管理.

本模块是 Athena 系统的核心编排层，负责将用户消息路由到合适的执行路径，
并协调记忆注入、上下文压缩、事实提取、阈值摘要等子系统。

核心组件：
- ``AgentWorkflow``  — 消息处理的顶层编排入口，串联记忆检索 → Harness 执行
  → 事实提取 → 阈值摘要的完整流水线。
- ``SubAgentManager`` — 子 Agent 生命周期管理器，支持串行 / 并行子任务派生，
  与父级共享工具集与审批队列。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
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
from athena.gateway.ws.manager import WebSocketManager
from athena.models import Message, MessageRole
from athena.models.file import Attachment, AttachmentRef, AttachmentStatus
from athena.gateway.ws.events import EventType, build_event
from athena.utils.ids import generate_sub_run_id, generate_time_id
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

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


@dataclass(frozen=True)
class _PreparedRun:
    """表示 PreparedRun 组件，封装相关状态和行为。
    """
    run_id: str
    user_message: Message
    attachment_refs: list[AttachmentRef]
    messages: list[Message]


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
        ws_manager: WebSocket 管理器，用于推送子 Agent 生命周期事件。
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
        ws_manager: WebSocketManager,
        compressor: ContextCompressor,
        memory_manager: MemoryManager,
        settings: Settings,
        main_run_id: str,
    ) -> None:
        """初始化当前对象。

        参数：
            llm (LLMProvider): 输入参数；其类型和取值约束由方法签名及实现定义。
            tool_manager (UnifiedToolManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            db (数据库): 输入参数；其类型和取值约束由方法签名及实现定义。
            ws_manager (WebSocketManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            compressor (ContextCompressor): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_manager (MemoryManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            settings (Settings): 全局配置对象。
            main_run_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._ws = ws_manager
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
        但共享父级的工具集和审批队列。执行完成后通过 WebSocket 推送
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
        await self._ws.send_to_session(
            session_id,
            build_event(
                EventType.SUB_AGENT_SPAWNED,
                {"task": task, "sub_run_id": sub_run_id, "max_turns": max_turns},
                session_id=session_id,
                run_id=sub_run_id,
            ),
        )

        # 子 Agent 使用过滤后的工具集（共享同一 manager，但通过 tool_names 白名单
        # 过滤 bind_tools 绑定与执行路径，allowed_tools=None 表示不限制）
        sub_harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            ws_manager=self._ws,
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
            await self._ws.send_to_session(
                session_id,
                build_event(
                    EventType.SUB_AGENT_COMPLETE,
                    {
                        "task": task,
                        "sub_run_id": sub_run_id,
                        "turn_count": result.turn_count,
                    },
                    session_id=session_id,
                    run_id=sub_run_id,
                ),
            )
            return sub_result
        except Exception as e:
            logger.exception("sub_agent_failed", sub_run_id=sub_run_id)
            await self._ws.send_to_session(
                session_id,
                build_event(
                    EventType.SUB_AGENT_FAILED,
                    {"task": task, "sub_run_id": sub_run_id, "error": str(e)},
                    session_id=session_id,
                    run_id=sub_run_id,
                ),
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
            """执行“使用信号量派生子 Agent”操作。

            参数：
                task (str): 输入参数；其类型和取值约束由方法签名及实现定义。

            返回值：
                SubAgentResult: 操作结果；具体语义由调用场景决定。

            异常：
                Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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


class AgentWorkflow:
    """Agent 工作流编排入口。

    负责将用户消息路由到 Harness 执行引擎，并协调以下子系统：

    - **记忆检索**：从长期记忆中召回与当前消息相关的上下文，注入系统提示。
    - **上下文压缩**：通过增量摘要控制 LLM 的上下文窗口大小。
    - **事实提取**：异步从用户消息中提取原子事实，写入长期记忆。
    - **阈值摘要**：每 N 轮对话触发一次摘要，丰富长期记忆中的对话概览。
    - **子 Agent 派生**：将可并行的子任务委托给独立的子 Agent 执行。

    生命周期：
        每次 ``process_message()`` 调用构成一次完整的编排流水线，
        从记忆注入到结果返回，全程不阻塞主事件循环。
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        db: Database,
        ws_manager: WebSocketManager,
        compressor: ContextCompressor,
        memory_retrieval: MemoryRetrievalService,
        conversation_summarizer: ConversationSummarizer,
        fact_extractor: FactExtractor,
        memory_manager: MemoryManager,
        settings: Settings,
    ) -> None:
        """初始化当前对象。

        参数：
            llm (LLMProvider): 输入参数；其类型和取值约束由方法签名及实现定义。
            tool_manager (UnifiedToolManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            db (数据库): 输入参数；其类型和取值约束由方法签名及实现定义。
            ws_manager (WebSocketManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            compressor (ContextCompressor): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_retrieval (MemoryRetrievalService): 输入参数；其类型和取值约束由方法签名及实现定义。
            conversation_summarizer (ConversationSummarizer): 输入参数；其类型和取值约束由方法签名及实现定义。
            fact_extractor (FactExtractor): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_manager (MemoryManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            settings (Settings): 全局配置对象。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
        self._memory_retrieval = memory_retrieval
        self._conversation_summarizer = conversation_summarizer
        self._fact_extractor = fact_extractor
        self._memory_manager = memory_manager
        self._settings = settings
        # 会话级停止事件，由 gateway 层注入，透传给 Harness 及子 Agent。
        self._session_stop_signals: dict[str, asyncio.Event | None] = {}

    def delegation_tool_specs(self):
        """返回用于组合根注册的 Agent 委派声明。"""
        from athena.core.tools.providers.agents import build_agent_tool_specs

        return build_agent_tool_specs(
            self._spawn_sub_agent_handler,
            self._spawn_parallel_handler,
            self._build_parallel_spawn_description(),
        )

    async def process_message(
        self,
        session_id: str,
        user_message: str,
        system_prompt: str | None = None,
        attachment_ids: list[str] | None = None,
        stop_signal: asyncio.Event | None = None,
        continuation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行消息处理流水线，并返回 Harness 结果或文件等待状态。"""
        user_message, attachment_ids = self._normalize_request(
            user_message, attachment_ids, continuation
        )
        requested_attachments = await self._load_requested_attachments(
            session_id, attachment_ids, continuation
        )
        self._session_stop_signals[session_id] = stop_signal
        logger.info(
            "task_classification_start",
            session_id=session_id,
            available_tools=self._tool_manager.list_names(),
        )
        memory_context = await self._retrieve_memory_context(session_id, user_message)
        history = await self._load_history(session_id)
        prepared = await self._prepare_run(
            session_id,
            user_message,
            attachment_ids,
            history,
            continuation,
        )
        waiting_result = await self._defer_for_pending_attachments(
            session_id,
            user_message,
            attachment_ids,
            requested_attachments,
            prepared,
            continuation,
        )
        if waiting_result is not None:
            return waiting_result

        result = await self._run_harness(
            prepared,
            session_id,
            self._build_system_prompt(system_prompt, memory_context),
            stop_signal,
        )
        await self._post_process(session_id, user_message, result)
        return self._result_payload(result, prepared.attachment_refs)

    @staticmethod
    def _normalize_request(
        user_message: str,
        attachment_ids: list[str] | None,
        continuation: dict[str, Any] | None,
    ) -> tuple[str, list[str]]:
        """执行“normalize request”操作。

        参数：
            user_message (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            attachment_ids (list[str] | None): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            tuple[str, list[str]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if continuation is not None:
            user_message = str(continuation.get("user_message", user_message))
            attachment_ids = list(
                continuation.get("attachment_ids", attachment_ids or [])
            )
        return user_message, list(dict.fromkeys(attachment_ids or []))

    async def _load_requested_attachments(
        self,
        session_id: str,
        attachment_ids: list[str],
        continuation: dict[str, Any] | None,
    ) -> list[Attachment]:
        """执行“加载请求的附件”操作。

        参数：
            session_id (str): 会话唯一标识。
            attachment_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[Attachment]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if not attachment_ids:
            return []
        loaded = await self._db.files.get_attachments(session_id, attachment_ids)
        attachments_by_id = {item.id: item for item in loaded}
        requested: list[Attachment] = []
        for file_id in attachment_ids:
            attachment = attachments_by_id.get(file_id)
            if attachment is None:
                deleted_during_wait = (
                    continuation is not None
                    and continuation.get("file_outcomes", {}).get(file_id)
                    == AttachmentStatus.DELETED.value
                )
                if deleted_during_wait:
                    continue
                raise ValueError("附件不存在或不属于当前会话")
            if continuation is None and attachment.status == AttachmentStatus.FAILED:
                raise ValueError(f"附件 {attachment.filename} 处理失败，不能随消息提交")
            requested.append(attachment)
        return requested

    async def _retrieve_memory_context(self, session_id: str, user_message: str) -> str:
        """执行“检索记忆上下文”操作。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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

    async def _load_history(self, session_id: str) -> list[Message]:
        """执行“加载历史记录”操作。

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[Message]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        session = await self._db.sessions.get(session_id)
        if not session or not (
            session.compression_summary and session.last_compressed_message_id
        ):
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

    @staticmethod
    def _build_system_prompt(system_prompt: str | None, memory_context: str) -> str:
        """执行“构建系统提示词”操作。

        参数：
            system_prompt (str | None): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_context (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        return (prompt + "\n\n" + memory_context).strip() if memory_context else prompt

    async def _prepare_run(
        self,
        session_id: str,
        user_message: str,
        attachment_ids: list[str],
        history: list[Message],
        continuation: dict[str, Any] | None,
    ) -> _PreparedRun:
        """执行“准备运行”操作。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            attachment_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。
            history (list[Message]): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            _PreparedRun: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        run_id = (
            str(continuation.get("run_id"))
            if continuation is not None
            else generate_time_id()
        )
        message = await self._get_or_create_user_message(
            session_id, user_message, run_id, history, continuation
        )
        attachment_refs = await self._bind_message_attachments(
            session_id, message, attachment_ids, continuation
        )
        messages = self._build_harness_messages(history, message, continuation)
        return _PreparedRun(run_id, message, attachment_refs, messages)

    async def _get_or_create_user_message(
        self,
        session_id: str,
        content: str,
        run_id: str,
        history: list[Message],
        continuation: dict[str, Any] | None,
    ) -> Message:
        """执行“获取或创建用户消息”操作。

        参数：
            session_id (str): 会话唯一标识。
            content (str): 待保存或处理的内容。
            run_id (str): 运行唯一标识。
            history (list[Message]): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Message: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if continuation is not None:
            message_id = continuation.get("message_id")
            existing = next((item for item in history if item.id == message_id), None)
            if existing is not None:
                return existing
            return Message(
                id=str(continuation.get("message_id", generate_time_id())),
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
        continuation: dict[str, Any] | None,
    ) -> list[AttachmentRef]:
        """执行“绑定消息附件”操作。

        参数：
            session_id (str): 会话唯一标识。
            message (Message): 输入参数；其类型和取值约束由方法签名及实现定义。
            attachment_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[AttachmentRef]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if continuation is None:
            if not attachment_ids:
                return []
            attachments = await self._db.files.bind_message(
                session_id, message.id, attachment_ids
            )
            refs = [item.to_ref() for item in attachments]
            message.attachments = refs
            return refs

        refs = message.attachments
        if refs:
            return refs
        attachments = (await self._db.files.attachments_for_messages([message.id])).get(
            message.id, []
        )
        refs = [item.to_ref() for item in attachments]
        message.attachments = refs
        return refs

    @staticmethod
    def _build_harness_messages(
        history: list[Message],
        user_message: Message,
        continuation: dict[str, Any] | None,
    ) -> list[Message]:
        """执行“构建 Harness 消息”操作。

        参数：
            history (list[Message]): 输入参数；其类型和取值约束由方法签名及实现定义。
            user_message (Message): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[Message]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if continuation is None:
            base_messages = [*history, user_message]
        else:
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

    async def _defer_for_pending_attachments(
        self,
        session_id: str,
        user_message: str,
        attachment_ids: list[str],
        requested_attachments: list[Attachment],
        prepared: _PreparedRun,
        continuation: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """执行“延迟处理待完成附件”操作。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            attachment_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。
            requested_attachments (list[Attachment]): 输入参数；其类型和取值约束由方法签名及实现定义。
            prepared (_PreparedRun): 输入参数；其类型和取值约束由方法签名及实现定义。
            continuation (dict[str, Any] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            dict[str, Any] | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if continuation is not None or not attachment_ids:
            return None
        tasks_by_attachment = await self._db.files.list_tasks_for_attachments(
            session_id, attachment_ids
        )
        pending_tasks = [
            task
            for file_id in attachment_ids
            for task in tasks_by_attachment.get(file_id, [])
            if task.status.value in ("queued", "running", "waiting")
        ]
        pending_file_ids = [
            attachment.id
            for attachment in requested_attachments
            if attachment.status != AttachmentStatus.READY
        ]
        if not pending_file_ids:
            return None

        task_ids = [task.id for task in pending_tasks]
        await self._db.files.create_continuation(
            session_id,
            prepared.run_id,
            task_ids=task_ids,
            request={
                "message_id": prepared.user_message.id,
                "user_message": user_message,
                "attachment_ids": attachment_ids,
            },
        )
        await self._db.sessions.update(
            session_id, status="waiting", run_id=prepared.run_id
        )
        await self._ws.send_to_session(
            session_id,
            build_event(
                EventType.AGENT_WAITING_FILE,
                {"file_ids": pending_file_ids, "task_ids": task_ids},
                session_id=session_id,
                run_id=prepared.run_id,
            ),
        )
        return {
            "content": "附件正在处理中，完成后将自动继续。",
            "run_id": prepared.run_id,
            "turn_count": 0,
            "tool_results": [],
            "error": None,
            "interrupted": False,
            "waiting": True,
            "attachments": self._serialize_attachments(prepared.attachment_refs),
        }

    async def _run_harness(
        self,
        prepared: _PreparedRun,
        session_id: str,
        system_prompt: str,
        stop_signal: asyncio.Event | None,
    ) -> HarnessRunResult:
        """执行“运行 Harness”操作。

        参数：
            prepared (_PreparedRun): 输入参数；其类型和取值约束由方法签名及实现定义。
            session_id (str): 会话唯一标识。
            system_prompt (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            stop_signal (asyncio.Event | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            HarnessRunResult: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            ws_manager=self._ws,
            compressor=self._compressor,
        )
        result = await harness.run(
            messages=prepared.messages,
            session_id=session_id,
            system_prompt=system_prompt,
            run_id=prepared.run_id,
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

    async def _post_process(
        self, session_id: str, user_message: str, result: HarnessRunResult
    ) -> None:
        """执行“后处理”操作。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            result (HarnessRunResult): 底层操作结果。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
    def _result_payload(
        cls, result: HarnessRunResult, attachment_refs: list[AttachmentRef]
    ) -> dict[str, Any]:
        """执行“结果负载”操作。

        参数：
            result (HarnessRunResult): 底层操作结果。
            attachment_refs (list[AttachmentRef]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            dict[str, Any]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
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
    def _serialize_attachments(refs: list[AttachmentRef]) -> list[dict[str, Any]]:
        """执行“序列化附件”操作。

        参数：
            refs (list[AttachmentRef]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            list[dict[str, Any]]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return [item.model_dump(mode="json") for item in refs]

    async def resume_file_continuation(self, continuation: dict[str, Any]) -> None:
        """索引任务完成后，仅恢复一次等待文件的运行。"""
        try:
            outcomes = continuation.get("file_outcomes", {})
            unavailable = {
                file_id: status
                for file_id, status in outcomes.items()
                if status
                in {AttachmentStatus.FAILED.value, AttachmentStatus.DELETED.value}
            }
            if unavailable:
                attachments = await self._db.files.get_attachments(
                    continuation["session_id"],
                    continuation.get("attachment_ids", []),
                    include_deleted=True,
                )
                names = {item.id: item.filename for item in attachments}
                details = "\n".join(
                    f"- {names.get(file_id, file_id)}: "
                    f"{'已删除' if status == AttachmentStatus.DELETED.value else '处理失败'}"
                    for file_id, status in unavailable.items()
                )
                content = (
                    "附件处理未完成，无法自动继续本次请求。\n"
                    f"{details}\n请重新上传文件或重试处理后再发送消息。"
                )
                await self._db.messages.save(
                    Message(
                        id=generate_time_id(),
                        session_id=continuation["session_id"],
                        role=MessageRole.ASSISTANT,
                        content=content,
                        run_id=str(continuation.get("run_id", "")),
                        timestamp=datetime.now(),
                    )
                )
                await self._db.sessions.update(
                    continuation["session_id"], status="idle"
                )
                await self._ws.send_to_session(
                    continuation["session_id"],
                    build_event(
                        EventType.SYSTEM_MESSAGE,
                        {
                            "content": content,
                            "attachment_ids": list(unavailable.keys()),
                        },
                        session_id=continuation["session_id"],
                        run_id=str(continuation.get("run_id", "")),
                    ),
                )
                await self._db.files.finish_continuation(
                    continuation["id"], failed=True
                )
                return
            await self.process_message(
                session_id=continuation["session_id"],
                user_message=continuation.get("user_message", ""),
                attachment_ids=continuation.get("attachment_ids", []),
                continuation=continuation,
            )
            await self._db.files.finish_continuation(continuation["id"])
        except Exception:
            await self._db.files.finish_continuation(continuation["id"], failed=True)
            logger.exception(
                "file_continuation_resume_failed",
                continuation_id=continuation.get("id"),
            )

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
        await self._ws.send_to_session(
            session_id,
            build_event(
                EventType.PARALLEL_AGENTS_STARTED,
                {"task_count": len(tasks), "tasks": [t[:500] for t in tasks]},
                session_id=session_id,
                run_id=parent_run_id,
            ),
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
            ws_manager=self._ws,
            compressor=self._compressor,
            memory_manager=self._memory_manager,
            settings=self._settings,
            main_run_id=main_run_id,
        )
