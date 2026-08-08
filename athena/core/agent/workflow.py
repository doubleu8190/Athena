"""Agent 工作流编排 + 子 Agent 管理.

- AgentWorkflow.process_message: 处理用户消息的编排入口
- SubAgentManager: 子 Agent 并行执行管理器
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
from athena.utils.ids import RunIdGenerator, generate_time_id
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
    """子 Agent 执行结果."""

    task: str
    content: str
    turn_count: int = 0
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    run_id: str | None = None


class SubAgentManager:
    """子 Agent 管理器 - 共享父 ApprovalManager 与工具集.

    project_memory 约束：子 Agent 通过依赖注入共享父 ApprovalManager，
    以保持顺序审批队列。
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
    ) -> SubAgentResult:
        """创建子 Agent 执行独立任务."""
        async with self._lock:
            self._sub_counter += 1
            index = self._sub_counter
        sub_run_id = (
            RunIdGenerator.generate_sub_run_id(self._main_run_id, index)
            if self._main_run_id
            else RunIdGenerator.generate_main_run_id()
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
                tool_names=allowed_tools,
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
    ) -> list[SubAgentResult]:
        """并行执行多个子任务."""
        results = await asyncio.gather(
            *[self.spawn(t, session_id, allowed_tools) for t in tasks],
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
    """Agent 工作流编排入口."""

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
        self._turn_counts: dict[str, int] = {}

        # 注册 spawn_sub_agent 工具，使 LLM 可通过 tool call 创建子 Agent
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
    ) -> dict[str, Any]:
        """处理用户消息的编排入口.

        流程：
        1. 注入相关记忆
        2. 调用 Harness 执行
        3. 异步提取事实
        4. 触发阈值摘要
        5. 持久化用户消息
        6. 返回结果
        """
        effective_prompt = DEFAULT_SYSTEM_PROMPT
        logger.info(
            "task_classification_start",
            session_id=session_id,
            available_tools=self._tool_manager.list_names(),
        )

        # 1. 注入相关记忆
        memory_context = ""
        try:
            memory_context = await self._memory_retrieval.get_relevant_memories(
                user_message=user_message,
                session_id=session_id,
            )
        except Exception as e:
            logger.warning("memory_injection_failed", error=str(e))

        # 2. 加载历史消息
        history: list[Message] = await self._db.get_messages(session_id)

        # 3. 构建系统提示（含记忆上下文）
        full_system_prompt = system_prompt or effective_prompt
        if memory_context:
            full_system_prompt = (full_system_prompt + "\n\n" + memory_context).strip()

        # 4. 调用 Harness
        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            ws_manager=self._ws,
            compressor=self._compressor,
        )
        user_msg = Message(
            id=generate_time_id(),  # 微秒级时间戳，单调递增且并发安全
            session_id=session_id,
            role=MessageRole.USER,
            content=user_message,
            metadata={},
            timestamp=datetime.now(),
        )
        messages_for_harness = history + [user_msg]
        # 5. 先持久化用户消息（中断恢复关键：run 中途崩溃时用户消息已在库中，
        #    避免出现没有对应用户消息的孤儿 assistant 消息）
        await self._db.save_message(user_msg)
        result = await harness.run(
            messages=messages_for_harness,
            session_id=session_id,
            system_prompt=full_system_prompt,
        )

        # 日志：记录任务分类决策结果（工具使用情况反映分类）
        logger.info(
            "task_classification_result",
            session_id=session_id,
            turn_count=result.turn_count,
            tool_count=len(result.tool_results),
            had_error=bool(result.error),
            interrupted=result.interrupted,
        )

        # 6. 异步提取事实（不阻塞主流程）
        asyncio.create_task(self._extract_facts_async(user_message, session_id))

        # 7. 触发阈值摘要
        self._turn_counts[session_id] = (
            self._turn_counts.get(session_id, 0) + result.turn_count
        )
        try:
            recent_messages = (
                history[-self._settings.summary_threshold * 2 :] if history else []
            )
            await self._conversation_summarizer.summarize_if_needed(
                session_id=session_id,
                turn_count=self._turn_counts[session_id],
                messages=recent_messages,
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

    async def _extract_facts_async(self, message: str, session_id: str) -> None:
        """异步提取事实（不阻塞主流程）."""
        try:
            await self._fact_extractor.extract(
                message, session_id, self._memory_manager
            )
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))

    async def _spawn_sub_agent_handler(self, task: str, session_id: str) -> str:
        """spawn_sub_agent 工具的执行处理器.

        创建子 Agent 执行独立子任务，返回子 Agent 的输出结果。
        失败时返回错误信息而非抛出异常，使 LLM 可以处理失败情况。
        """
        logger.info(
            "sub_agent_tool_invoked",
            session_id=session_id,
            task=task[:200],
        )
        try:
            manager = self.get_sub_agent_manager()
            result = await manager.spawn(task=task, session_id=session_id)
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
        """获取子 Agent 管理器实例（共享父级依赖）."""
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
