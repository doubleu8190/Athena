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
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from athena.config.settings import Settings, get_settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.core.memory.summarizer import ConversationSummarizer, FactExtractor
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.models import Message, MessageRole
from athena.schemas.events import EventType, build_event
from athena.utils.ids import generate_sub_run_id, generate_time_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# ── 默认系统提示词 — 三层任务分类 ──
# 工具定义（含审批/风险治理信息）统一由 bind_tools 的函数 schema 注入，
# 提示词内不再重复罗列工具列表。
DEFAULT_SYSTEM_PROMPT = """\
你是 Athena，一个采用三层任务分类系统的 AI 助手。
请将每个用户请求划分到以下三个层级之一，并据此采取行动。

## 第一层：直接回答
对于仅凭已有知识即可回答的简单问题（定义、解释、代码片段、数学、翻译）——直接回答，不调用任何工具。

## 第二层：工具调用
当任务需要外部信息或文件系统操作时，使用已绑定到你的工具。每个工具的风险等级和审批要求会标注在工具描述中。

工具错误处理：如果工具调用失败，使用修正后的参数重试一次。如果再次失败，向用户报告错误并建议替代方案——不要无限循环。

## 第三层：子 Agent 委派
对于适合拆解或并行执行的复杂任务（多步研究、多文件重构、包含相互独立子任务的任务），使用 spawn_sub_agent 工具创建专门的子 Agent。每个子 Agent 独立运行，拥有自己的工具访问权限，并返回你可以汇总的结果。

子 Agent 使用指南：
- 将任务拆解为相互独立、边界清晰的子任务
- 每个子任务派生一个子 Agent（它们并行运行）
- 汇总各子结果并综合成连贯的最终回答
- 如果某个子 Agent 失败，重试一次或报告部分结果

## 决策边界
- 默认使用第一层。仅当任务确实需要时才升级到第二/三层。
- 单个工具调用 = 第二层。多个可并行运行的独立工具调用 = 考虑第三层。
- 绝不为需要顺序推理或步骤间强耦合的任务使用第三层——改用带多轮对话的第二层。
"""


class SubAgentResult(BaseModel):
    """子 Agent 执行结果.

    Attributes:
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

    Args:
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
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
        self._memory_manager = memory_manager
        self._settings = settings or get_settings()
        self._main_run_id = main_run_id
        self._sub_counter = 0
        self._lock = asyncio.Lock()

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

        Args:
            task: 子任务的自然语言描述，直接作为子 Agent 的首轮用户消息。
            session_id: 当前会话 ID，用于事件推送和消息持久化。
            allowed_tools: 工具白名单；``None`` 表示不限制，继承父级全部工具。
            max_turns: 子 Agent 最大 LLM 交互轮次，默认 5。
            stop_signal: 停止事件，置位时终止子 Agent 执行。

        Returns:
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
                system_prompt=(
                    "你是一个子 Agent。完成分配给你的任务并返回结果。"
                    "请保持简洁和专注。"
                ),
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
        stop_signal: asyncio.Event | None = None,
    ) -> list[SubAgentResult]:
        """并行派生并执行多个子 Agent。

        使用 ``asyncio.gather`` 并发调度所有子任务，单个子 Agent 的异常
        不会影响其余子 Agent 的执行。

        Args:
            tasks: 子任务描述列表，每个元素对应一个子 Agent。
            session_id: 当前会话 ID。
            allowed_tools: 工具白名单，所有子 Agent 共享。
            stop_signal: 停止事件，置位时终止所有子 Agent。

        Returns:
            执行结果列表，长度 <= ``len(tasks)``。失败的子 Agent 结果
            以日志形式记录，不包含在返回值中。
        """
        results = await asyncio.gather(
            *[
                self.spawn(t, session_id, allowed_tools, stop_signal=stop_signal)
                for t in tasks
            ],
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
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
        self._memory_retrieval = memory_retrieval
        self._conversation_summarizer = conversation_summarizer
        self._fact_extractor = fact_extractor
        self._memory_manager = memory_manager
        self._settings = settings or get_settings()
        # 会话级停止事件，由 gateway 层注入，透传给 Harness 及子 Agent。
        self._session_stop_signals: dict[str, asyncio.Event | None] = {}

        # 注册 spawn_sub_agent native 工具，使 LLM 可通过 tool call 派生子 Agent。
        if "spawn_sub_agent" not in self._tool_manager._tools:
            self._tool_manager.register_native(
                name="spawn_sub_agent",
                description=(
                    "Create a sub-agent to handle an independent subtask. "
                    "Use this for complex tasks that can be decomposed into "
                    "parallel or independent subtasks. The sub-agent runs with "
                    "its own tool access and returns a result."
                ),
                handler=self._spawn_sub_agent_handler,
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The independent subtask to delegate to the sub-agent.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "The current session ID.",
                        },
                    },
                    "required": ["task", "session_id"],
                },
                risk_level="medium",
                require_approval=False,
            )

    async def process_message(
        self,
        session_id: str,
        user_message: str,
        system_prompt: str | None = None,
        stop_signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """处理用户消息的编排入口。

        完整流水线：
        1. 注入相关长期记忆到系统提示。
        2. 加载历史消息（优先使用摘要 + 增量消息，避免全量加载）。
        3. 构建系统提示（含记忆上下文）。
        4. 持久化用户消息（中断恢复保障）。
        5. 调用 Harness 执行 LLM 交互与工具调用。
        6. 异步提取事实（不阻塞主流程）。
        7. 触发阈值摘要（每 N 轮对话自动生成摘要）。

        Args:
            session_id: 会话唯一标识。
            user_message: 用户输入的原始文本。
            system_prompt: 自定义系统提示；为 ``None`` 时使用默认三层分类提示。
            stop_signal: 会话级停止事件，由 gateway 层注入，透传给 Harness
                及子 Agent（任一置位即终止运行）。

        Returns:
            包含以下字段的字典：
            - ``content`` (str): 助手回复的最终文本内容。
            - ``run_id`` (str): 本次运行的唯一标识。
            - ``turn_count`` (int): LLM 交互轮次。
            - ``tool_results`` (list[dict]): 工具调用结果列表。
            - ``error`` (str | None): 错误信息；成功时为 ``None``。
            - ``interrupted`` (bool): 是否因停止信号而中断。
        """
        effective_prompt = DEFAULT_SYSTEM_PROMPT
        self._session_stop_signals[session_id] = stop_signal
        logger.info(
            "task_classification_start",
            session_id=session_id,
            available_tools=self._tool_manager.list_names(),
        )

        # ── Step 1: 记忆检索 ──
        # 从长期记忆中召回与当前消息相关的上下文，注入系统提示。
        memory_context = ""
        try:
            memory_context = await self._memory_retrieval.get_relevant_memories(
                user_message=user_message,
            )
            logger.info(
                "memory_injection_success",
                session_id=session_id,
                memory_context=memory_context,
            )
        except Exception as e:
            logger.warning("memory_injection_failed", error=str(e))

        # ── Step 2: 加载历史消息 ──
        # 优先使用压缩摘要 + 增量消息，避免随对话增长而全量加载。
        # 首次压缩由 ContextCompressor 在 Harness 循环中触发并持久化摘要；
        # 后续调用直接复用已有摘要，只加载压缩点之后的增量消息。
        session = await self._db.sessions.get(session_id)
        compression_summary = session.compression_summary if session else None
        last_compressed_id = session.last_compressed_message_id if session else None

        if compression_summary and last_compressed_id:
            history_after = await self._db.messages.get_after_message(
                session_id, last_compressed_id,
            )
            summary_msg = Message(
                id=generate_time_id(),
                session_id=session_id,
                role=MessageRole.SYSTEM,
                content=f"[对话历史摘要]\n{compression_summary}",
                metadata={"type": "conversation_summary"},
                timestamp=datetime.now(),
            )
            history: list[Message] = [summary_msg] + history_after
            logger.info(
                "history_loaded_with_summary",
                session_id=session_id,
                incremental_count=len(history_after),
            )
        else:
            history: list[Message] = await self._db.messages.get_by_session(session_id)

        # ── Step 3: 构建系统提示 ──
        full_system_prompt = system_prompt or effective_prompt
        if memory_context:
            full_system_prompt = (full_system_prompt + "\n\n" + memory_context).strip()

        # ── Step 4: 生成 run_id 并持久化用户消息 ──
        # run_id 在持久化之前生成，使用户消息与 steps 共享同一分组键：
        # 前端据此把工具/步骤按"用户请求"归组；同时避免旧 run_id
        # 按日期+计数方案在进程重启后同一天重号的问题。
        rid = generate_time_id()

        # 中断恢复保障：run 中途崩溃时用户消息已在库中，
        # 避免出现没有对应用户消息的孤儿 assistant 消息。
        user_msg = Message(
            id=generate_time_id(),
            session_id=session_id,
            role=MessageRole.USER,
            content=user_message,
            run_id=rid,
            metadata={},
            timestamp=datetime.now(),
        )
        await self._db.messages.save(user_msg)
        messages_for_harness = history + [user_msg]

        # ── Step 5: 调用 Harness 执行 LLM 交互与工具调用 ──
        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            ws_manager=self._ws,
            compressor=self._compressor,
        )
        result = await harness.run(
            messages=messages_for_harness,
            session_id=session_id,
            system_prompt=full_system_prompt,
            run_id=rid,
            stop_signal=stop_signal,
        )

        # 工具使用情况反映了三层分类的决策结果，记录用于可观测性。
        logger.info(
            "task_classification_result",
            session_id=session_id,
            turn_count=result.turn_count,
            tool_count=len(result.tool_results),
            had_error=bool(result.error),
            interrupted=result.interrupted,
        )

        # ── Step 6: 异步提取事实 ──
        # 从用户消息中提取原子事实写入长期记忆，不阻塞主流程。
        asyncio.create_task(self._extract_facts_async(user_message, result.content, session_id))

        # ── Step 7: 阈值摘要（增量，按完整轮次触发） ──
        # summarizer 自行从 DB 加载增量消息，按完整对话轮次边界处理，
        # 通过 last_summarized_message_id 指针保证每条消息恰好处理一次。
        try:
            await self._conversation_summarizer.summarize_if_needed(
                session_id=session_id,
                db=self._db,
            )
        except Exception as e:
            logger.warning("summary_trigger_failed", error=str(e))

        return {
            "content": result.content,
            "run_id": result.run_id,
            "turn_count": result.turn_count,
            "tool_results": result.tool_results,
            "error": result.error,
            "interrupted": result.interrupted,
        }

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

        Args:
            user_message: 用户消息的原始文本。
            assistant_reply: 助手回复的文本内容；为空时退化为仅提取用户消息。
            session_id: 当前会话 ID。
        """
        try:
            await self._fact_extractor.extract(
                user_message, assistant_reply, session_id, self._memory_manager,
            )
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))

    async def _spawn_sub_agent_handler(
        self, task: str, session_id: str, parent_run_id: str | None = None
    ) -> str:
        """``spawn_sub_agent`` 工具的执行处理器。

        作为注册到 ``UnifiedToolManager`` 的 native handler 被 Harness 调用。
        创建子 Agent 执行独立子任务，返回其输出结果。失败时返回错误信息
        而非抛出异常，使 LLM 能够感知失败并决定是否重试或降级处理。

        Args:
            task: 子任务的自然语言描述。
            session_id: 当前会话 ID。
            parent_run_id: 父级运行 ID，由工具管理器从调用链注入
                （Harness 执行工具时携带自身 run_id）。子 Agent 据此生成
                带父链的 sub_run_id（``"{parent_run_id}_{index}"``），
                前端可将子任务的步骤归组到父请求下。

        Returns:
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

    def get_sub_agent_manager(self, main_run_id: str | None = None) -> SubAgentManager:
        """创建子 Agent 管理器实例。

        每次调用返回新实例，共享当前工作流的 LLM、工具集、数据库连接
        和压缩器等依赖。``main_run_id`` 用于生成子 Agent 的 ``sub_run_id``。

        Args:
            main_run_id: 父级运行 ID；为 ``None`` 时子 Agent 使用独立的时间戳 ID。

        Returns:
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
