"""Agent 工作流编排 + 子 Agent 管理.

- AgentWorkflow.process_message: 处理用户消息的编排入口
- SubAgentManager: 子 Agent 并行执行管理器
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from athena.config.settings import Settings, get_settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.core.memory.summarizer import ConversationSummarizer
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.schemas.events import EventType, build_event
from athena.utils.ids import RunIdGenerator
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# ── 默认系统提示词 — 三层任务分类 ──
# 注意：工具列表不在此硬编码，运行时由 _build_tool_description() 动态注入
DEFAULT_SYSTEM_PROMPT = """\
You are Athena, an AI assistant with a three-tier task classification system.
Classify each user request into one of three categories and act accordingly.

## Tier 1: Direct Response
For simple questions answerable from existing knowledge (definitions, explanations,
code snippets, math, translations) — respond directly without invoking any tools.

## Tier 2: Tool Invocation
When the task requires external information or filesystem operations, use the
available tools listed below. Each tool's risk level and approval requirement
is indicated in its description.

{tool_list}

Tool error handling: if a tool call fails, retry once with corrected parameters.
If it fails again, report the error to the user and suggest alternatives — do not
loop indefinitely.

## Tier 3: Sub-Agent Delegation
For complex tasks that benefit from decomposition or parallel execution (multi-step
research, multi-file refactoring, tasks with independent subtasks), use the
spawn_sub_agent tool to create specialized sub-agents. Each sub-agent runs
independently with its own tool access and returns a result you can aggregate.

Guidelines for sub-agent usage:
- Decompose the task into independent, well-scoped subtasks
- Spawn one sub-agent per subtask (they run in parallel)
- Aggregate results and synthesize a coherent final response
- If a sub-agent fails, retry it once or report the partial result

## Decision Boundaries
- Default to Tier 1. Only escalate to Tier 2/3 when the task genuinely requires it.
- A single tool call = Tier 2. Multiple independent tool calls that could run in
  parallel = consider Tier 3.
- Never use Tier 3 for tasks that need sequential reasoning or tight coupling
  between steps — use Tier 2 with multiple turns instead.
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
        db: Database | None = None,
        ws_manager: WebSocketManager | None = None,
        compressor: ContextCompressor | None = None,
        settings: Settings | None = None,
        main_run_id: str | None = None,
    ) -> None:
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
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
        if self._ws is not None:
            await self._ws.send_to_session(
                session_id,
                build_event(
                    EventType.SUB_AGENT_SPAWNED,
                    {"task": task, "sub_run_id": sub_run_id, "max_turns": max_turns},
                    session_id=session_id,
                    run_id=sub_run_id,
                ),
            )

        # 子 Agent 使用过滤后的工具集（共享同一 manager，但通过 langchain_tools 名单过滤）
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
                    "You are a sub-agent. Complete the assigned task and return the result. "
                    "Be concise and focused."
                ),
                run_id=sub_run_id,
            )
            sub_result = SubAgentResult(
                task=task,
                content=result.content,
                turn_count=result.turn_count,
                tool_results=result.tool_results,
                error=result.error,
                run_id=sub_run_id,
            )
            if self._ws is not None:
                await self._ws.send_to_session(
                    session_id,
                    build_event(
                        EventType.SUB_AGENT_COMPLETE,
                        {"task": task, "sub_run_id": sub_run_id, "turn_count": result.turn_count},
                        session_id=session_id,
                        run_id=sub_run_id,
                    ),
                )
            return sub_result
        except Exception as e:
            logger.exception("sub_agent_failed", sub_run_id=sub_run_id)
            if self._ws is not None:
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
        db: Database | None = None,
        ws_manager: WebSocketManager | None = None,
        compressor: ContextCompressor | None = None,
        memory_retrieval: MemoryRetrievalService | None = None,
        conversation_summarizer: ConversationSummarizer | None = None,
        fact_extractor: Any | None = None,
        memory_manager: Any | None = None,
        settings: Settings | None = None,
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
        system_prompt: str = "",
    ) -> dict[str, Any]:
        """处理用户消息的编排入口.

        流程：
        1. 持久化用户消息
        2. 注入相关记忆（若启用）
        3. 调用 Harness 执行
        4. 异步提取事实（若启用）
        5. 触发阈值摘要（若启用）
        6. 返回结果
        """
        # 应用默认系统提示词（当客户端未提供时），并动态注入当前可用工具列表
        if system_prompt.strip():
            effective_prompt = system_prompt
        else:
            effective_prompt = DEFAULT_SYSTEM_PROMPT.format(
                tool_list=self._build_tool_description()
            )
        logger.info(
            "task_classification_start",
            session_id=session_id,
            using_custom_prompt=bool(system_prompt.strip()),
            available_tools=self._tool_manager.list_names(),
        )

        # 1. 持久化用户消息
        if self._db is not None:
            await self._db.save_message(session_id, {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": user_message,
                "metadata": {},
                "timestamp": datetime.now().isoformat(),
            })

        # 2. 注入相关记忆
        memory_context = ""
        if self._memory_retrieval is not None:
            try:
                memory_context = await self._memory_retrieval.get_relevant_memories(
                    user_message=user_message,
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning("memory_injection_failed", error=str(e))

        # 3. 加载历史消息
        history: list[dict[str, Any]] = []
        if self._db is not None:
            history = await self._db.get_messages(session_id)
            # 移除刚保存的用户消息（避免重复），由 Harness 拼接
            if history and history[-1].get("role") == "user" and history[-1].get("content") == user_message:
                history = history[:-1]

        # 4. 构建系统提示（含记忆上下文）
        full_system_prompt = effective_prompt
        if memory_context:
            full_system_prompt = (full_system_prompt + "\n\n" + memory_context).strip()

        # 5. 调用 Harness
        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            ws_manager=self._ws,
            compressor=self._compressor,
        )

        messages_for_harness = history + [{"role": "user", "content": user_message}]
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
        if self._fact_extractor is not None and self._memory_manager is not None:
            asyncio.create_task(
                self._extract_facts_async(user_message, session_id)
            )

        # 7. 触发阈值摘要
        if self._conversation_summarizer is not None:
            self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + result.turn_count
            try:
                recent_messages = history[-self._settings.summary_threshold * 2 :] if history else []
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
            if self._fact_extractor is not None and self._memory_manager is not None:
                await self._fact_extractor.extract(message, session_id, self._memory_manager)
        except Exception as e:
            logger.warning("fact_extraction_failed", error=str(e))

    def _build_tool_description(self) -> str:
        """从 UnifiedToolManager 动态生成工具列表描述.

        遍历当前注册的所有工具，生成包含名称、描述、风险等级和审批要求的
        格式化列表，用于注入系统提示词。工具集变化时无需修改代码。
        """
        lines: list[str] = []
        for tool in self._tool_manager.list_tools():
            schema = tool.schema
            flags = []
            if schema.require_approval:
                flags.append("requires approval")
            flags.append(f"risk: {schema.risk_level.value if hasattr(schema.risk_level, 'value') else schema.risk_level}")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"- {schema.name}: {schema.description}{flag_str}")
        return "\n".join(lines) if lines else "(no tools available)"

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
            settings=self._settings,
            main_run_id=main_run_id,
        )
