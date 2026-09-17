"""基于 LangGraph 的 Agent 运行时。

本模块是 Athena 系统的核心编排层，负责依赖组装和服务注入。
领域逻辑已拆分至 ``services/`` 下的聚焦服务模块。

核心组件：
- ``LangGraphRuntime`` — 依赖组装器，创建并持有各项服务，对外暴露统一入口。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from typing import Any

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.harness.harness import HarnessRunResult
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.core.memory.summarizer import ConversationSummarizer, FactExtractor
from athena.core.memory.trigger import MemoryTrigger
from athena.core.memory.workflow import MemoryWriteWorkflow
from athena.core.memory.resolver import MemoryResolver
from athena.infrastructure.sqlite.memory_job_repository import MemoryJobRepository
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from athena.models import Message
from athena.models.file import Attachment, AttachmentRef, AttachmentStatus
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import EventPublisherPort
from athena.contracts.ports import AgentStorePort
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

from .attachment_processor import AttachmentProcessContext, AttachmentProcessor
from .processors import StaticProcessorProxy
from .state import AgentState, FileProcessResult
from .sub_agent import SubAgentManager
from .services.session_context_service import SessionContextService
from .services.execution_service import ExecutionService
from .orchestration.events import OrchestrationEventPublisher
from .orchestration import (
    Dispatcher,
    ExecutionPlan,
    PlanStatus,
    Planner,
    StructuredLLMService,
    Synthesizer,
    WorkerExecutor,
)
from .task_understanding import TaskUnderstandingService
from .context import ContextAcquisitionService, ContextPlanner
from .context.providers.file import FileContextProvider
from .context.providers.knowledge import KnowledgeContextProvider
from .context.providers.memory import MemoryContextProvider

logger = get_logger(__name__)

# ── 默认系统提示词 — 从 prompt/system.md 加载 ──
DEFAULT_SYSTEM_PROMPT = get_prompt("system")


class LangGraphRuntime:
    """为 LangGraph 提供领域执行能力的依赖组装器。

    创建并持有所有聚焦服务，向 Graph Node 提供统一的委托入口。
    领域逻辑已拆分至：
    - ``TaskUnderstandingService`` — 用户任务理解
    - ``ContextAcquisitionService`` — Memory / Knowledge / File 上下文获取
    - ``SessionContextService`` — 会话历史、消息持久化、附件绑定和 Harness 输入准备
    - ``ExecutionService`` — Harness 执行编排与后处理

    保留在运行时的职责：
    - 附件处理（依赖 ``FileIntelligenceRuntime`` 的信号量和生命周期）
    - 子 Agent 工具处理器（依赖会话级停止信号）
    - 依赖组装与验证
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
        file_runtime: FileIntelligenceRuntime,
        memory_job_repository: MemoryJobRepository,
        agent_store: AgentStorePort,
    ) -> None:
        """组装所有聚焦服务并保留直接依赖。

        参数：
            llm: LLM 提供者。
            tool_manager: 统一工具管理器。
            db: 数据库连接。
            event_publisher: 应用事件发布器。
            compressor: 上下文压缩器。
            memory_retrieval: 记忆检索服务。
            conversation_summarizer: 对话摘要生成器。
            fact_extractor: 事实提取器。
            memory_manager: 长期记忆管理器。
            settings: 全局配置。
            file_runtime: 文件智能运行时。
            memory_job_repository: 记忆任务持久化仓库。
        """
        # ── 聚焦服务 ──
        structured_llm = StructuredLLMService(
            llm,
            timeout_seconds=settings.task_understanding_timeout_seconds,
        )
        self._task_understanding_service = TaskUnderstandingService(structured_llm)
        memory_provider = MemoryContextProvider(
            memory_retrieval,
            timeout_seconds=settings.memory_retrieval_timeout_seconds,
        )
        knowledge_provider = KnowledgeContextProvider(
            db.files,
            file_runtime,
            timeout_seconds=settings.memory_retrieval_timeout_seconds,
        )
        file_provider = FileContextProvider(
            file_runtime,
            timeout_seconds=settings.memory_retrieval_timeout_seconds,
        )
        self._context_acquisition_service = ContextAcquisitionService(
            memory_provider=memory_provider,
            knowledge_provider=knowledge_provider,
            file_provider=file_provider,
            token_counter=llm,
        )
        self._context_planner = ContextPlanner()
        self._session_context_service = SessionContextService(
            db=db, event_publisher=event_publisher
        )
        self._execution_service = ExecutionService(
            llm=llm,
            tool_manager=tool_manager,
            db=db,
            compressor=compressor,
            memory_manager=memory_manager,
            conversation_summarizer=conversation_summarizer,
            memory_job_repository=memory_job_repository,
            settings=settings,
            event_publisher=event_publisher,
        )

        # ── 直接依赖（用于附件处理、子 Agent、图组装） ──
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._events = event_publisher
        self._compressor = compressor
        self._memory_manager = memory_manager
        self._conversation_summarizer = conversation_summarizer
        self._fact_extractor = fact_extractor
        self._settings = settings
        self._file_runtime = file_runtime
        self._memory_job_repository = memory_job_repository
        self._agent_store = agent_store

        # ── 运行时状态 ──
        self._session_stop_signals: dict[str, asyncio.Event | None] = {}
        self._file_parse_semaphore = asyncio.Semaphore(2)
        self._file_embedding_semaphore = asyncio.Semaphore(3)
        self._memory_trigger = MemoryTrigger()
        self._memory_resolver = MemoryResolver(self._memory_manager)
        self._memory_write_workflow: MemoryWriteWorkflow | None = None
        structured_llm = StructuredLLMService(llm)
        orchestration_events = OrchestrationEventPublisher(event_publisher)
        self._orchestration_planner = Planner(db, orchestration_events)
        self._orchestration_worker = WorkerExecutor(
            llm=llm,
            structured_llm=structured_llm,
            tool_manager=tool_manager,
            db=db,
            compressor=compressor,
            events=event_publisher,
            agent_store=agent_store,
            settings=settings,
            root_run_id="",
        )
        self._orchestration_dispatcher = Dispatcher(
            db, self._orchestration_worker, orchestration_events
        )
        self._orchestration_synthesizer = Synthesizer(llm)
        self._orchestration_events = orchestration_events

    # ------------------------------------------------------------------
    # 属性暴露（供 langgraph_graph.py 注入到节点）
    # ------------------------------------------------------------------

    @property
    def task_understanding_service(self) -> TaskUnderstandingService:
        return self._task_understanding_service

    @property
    def context_acquisition_service(self) -> ContextAcquisitionService:
        return self._context_acquisition_service

    @property
    def context_planner(self) -> ContextPlanner:
        return self._context_planner

    @property
    def session_context_service(self) -> SessionContextService:
        return self._session_context_service

    @property
    def execution_service(self) -> ExecutionService:
        return self._execution_service

    @property
    def orchestration_planner(self) -> Planner:
        return self._orchestration_planner

    @property
    def orchestration_dispatcher(self) -> Dispatcher:
        return self._orchestration_dispatcher

    @property
    def orchestration_synthesizer(self) -> Synthesizer:
        return self._orchestration_synthesizer

    async def materialize_plan(
        self,
        *,
        session_id: str,
        root_run_id: str,
        user_goal: str,
        submission: dict[str, Any],
    ) -> ExecutionPlan:
        """校验并持久化顶层 Agent LLM 提交的计划。"""
        return await self._orchestration_planner.materialize_submission(
            session_id=session_id,
            root_run_id=root_run_id,
            user_goal=user_goal,
            submission=submission,
            available_tools=self._tool_manager.list_names(),
        )

    async def execute_planned_orchestration(
        self,
        *,
        plan_payload: dict[str, Any],
        session_id: str,
        stop_signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """执行已持久化的计划并返回汇总结果。"""
        from .orchestration import ExecutionPlan

        plan = ExecutionPlan.model_validate(plan_payload)
        self._orchestration_dispatcher.register_session(plan.plan_id, session_id)
        try:
            await self._db.orchestration.set_plan_status(
                plan.plan_id, PlanStatus.SYNTHESIZING.value
            )
            results = await self._orchestration_dispatcher.run(
                plan=plan,
                session_id=session_id,
                stop_signal=stop_signal,
            )
            content = await self._orchestration_synthesizer.synthesize(plan, results)
            await self._db.orchestration.set_plan_status(
                plan.plan_id,
                PlanStatus.COMPLETED.value,
            )
            await self._orchestration_events.publish_synthesis(
                EventType.SYNTHESIS_COMPLETED,
                session_id=session_id,
                plan_id=plan.plan_id,
                run_id=plan.root_run_id,
                payload={"task_count": len(plan.tasks)},
            )
            return {
                "content": content,
                "run_id": plan.root_run_id,
                "mode": "execute_plan",
                "plan_id": plan.plan_id,
                "results": {
                    task_id: result.model_dump(mode="json")
                    for task_id, result in results.items()
                },
            }
        except Exception as exc:
            await self._db.orchestration.set_plan_status(
                plan.plan_id,
                PlanStatus.FAILED.value,
                error={"message": str(exc)},
            )
            await self._orchestration_events.publish_synthesis(
                EventType.PLAN_FAILED,
                session_id=session_id,
                plan_id=plan.plan_id,
                run_id=plan.root_run_id,
                payload={"error": str(exc)},
            )
            raise

    # ------------------------------------------------------------------
    # Task Understanding / Context 委托
    # ------------------------------------------------------------------

    async def understand_task(self, state: AgentState) -> AgentState:
        """生成当前请求的 UserTaskSpec。"""
        history = state.get("history", [])
        attachment_refs = state.get("requested_attachment_refs", [])
        knowledge_documents = await self._db.files.list_global_knowledge_documents()
        result = await self._task_understanding_service.understand(
            session_id=state.get("session_id", ""),
            user_message=state.get("user_message", ""),
            history=history,
            attachment_refs=attachment_refs,
            knowledge_document_count=len(knowledge_documents),
            tool_names=self._tool_manager.list_names(),
        )
        update: AgentState = {
            "task_spec": result.task.model_dump(mode="json"),
            "task_understanding_source": result.source,
        }
        if result.task.requires_clarification:
            update["clarification_question"] = result.task.clarification_question
        return update

    async def complete_clarification(self, state: AgentState) -> AgentState:
        """把澄清问题持久化为一次普通 assistant 回答。"""
        from athena.models import Message, MessageRole

        session_id = state.get("session_id", "")
        run_id = state.get("run_id", "")
        message_id = state.get("message_id", "")
        clarification = state.get("clarification_question") or "请补充说明你的目标。"
        assistant_message_id = f"{run_id}:assistant:clarification"
        message = Message(
            id=assistant_message_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=clarification,
            run_id=run_id,
            timestamp=datetime.now(),
        )
        await self._db.messages.save(message)
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.STREAM_END,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                message_id=message_id or None,
                stream_id=f"answer:{run_id}",
                stream_type="answer",
                is_complete=True,
                payload={
                    "content": clarification,
                    "run_id": run_id,
                    "turn_count": 0,
                    "content_type": "clarification",
                },
            )
        )
        await self._db.sessions.update(session_id, status="idle")
        return {
            "result": {
                "content": clarification,
                "run_id": run_id,
                "turn_count": 0,
                "tool_results": [],
                "error": None,
                "error_detail": None,
                "interrupted": False,
                "attachments": state.get("requested_attachment_refs", []),
            }
        }

    def plan_context(self, state: AgentState) -> AgentState:
        """根据任务理解结果生成上下文获取计划。"""
        from athena.runtime.task_understanding import UserTaskSpec

        task_payload = state.get("task_spec")
        if task_payload is None:
            task = UserTaskSpec(
                goal=state.get("user_message", "")[:1000] or "answer the user",
                task_type="answer",
                context_requirements=["conversation"],
            )
        else:
            task = UserTaskSpec.model_validate(task_payload)
        requested_file_ids = [
            ref.get("id", "")
            for ref in state.get("requested_attachment_refs", [])
            if ref.get("id")
        ]
        plan = self._context_planner.plan(
            task,
            requested_file_ids=requested_file_ids,
            max_files=self._settings.knowledge_context_max_files,
            limit_per_file=self._settings.knowledge_context_limit_per_file,
            max_items=self._settings.knowledge_context_max_items,
            max_tokens=self._settings.knowledge_context_max_tokens,
        )
        return {"context_plan": plan.model_dump(mode="json")}

    async def acquire_context(self, state: AgentState) -> AgentState:
        """并发获取计划中的上下文。"""
        from athena.runtime.context.contracts import ContextPlan
        from athena.runtime.task_understanding import UserTaskSpec

        task_payload = state.get("task_spec")
        plan_payload = state.get("context_plan")
        task = (
            UserTaskSpec.model_validate(task_payload)
            if task_payload is not None
            else UserTaskSpec(
                goal=state.get("user_message", "")[:1000] or "answer the user",
                task_type="answer",
                context_requirements=["conversation"],
            )
        )
        plan = (
            ContextPlan.model_validate(plan_payload)
            if plan_payload is not None
            else ContextPlan()
        )
        bundle = await self._context_acquisition_service.acquire(
            session_id=state.get("session_id", ""),
            task=task,
            plan=plan,
        )
        return {"context_bundle": bundle.model_dump(mode="json")}

    # ------------------------------------------------------------------
    # SessionContextService 委托
    # ------------------------------------------------------------------

    async def load_history(self, session_id: str) -> list[Message]:
        return await self._session_context_service.load_history(session_id)

    async def load_and_validate_attachments(
        self, session_id: str, attachment_ids: list[str]
    ) -> list[Attachment]:
        return await self._session_context_service.load_and_validate_attachments(
            session_id, attachment_ids
        )

    async def prepare_harness_input(
        self,
        session_id: str,
        user_message: str,
        attachment_ids: list[str],
        history: list[Message],
        run_id: str = "",
        message_id: str = "",
    ) -> tuple[list[AttachmentRef], list[Message]]:
        return await self._session_context_service.prepare_harness_input(
            session_id, user_message, attachment_ids, history, run_id, message_id
        )

    # ------------------------------------------------------------------
    # ExecutionService 委托
    # ------------------------------------------------------------------

    @staticmethod
    def build_system_prompt(
        task_spec: dict[str, Any] | None = None,
        context_bundle: dict[str, Any] | None = None,
    ) -> str:
        """使用内置系统提示词，并注入任务理解和上下文包。"""
        return ExecutionService._build_system_prompt(task_spec, context_bundle)

    async def run_harness(
        self,
        messages: list[Message],
        session_id: str,
        task_spec: dict[str, Any] | None = None,
        context_bundle: dict[str, Any] | None = None,
        run_id: str = "",
        stop_signal: Any = None,
    ) -> HarnessRunResult:
        return await self._execution_service.run_harness(
            messages=messages,
            session_id=session_id,
            task_spec=task_spec,
            context_bundle=context_bundle,
            run_id=run_id,
            stop_signal=stop_signal,
        )

    async def post_process(
        self,
        session_id: str,
        user_message: str,
        result: HarnessRunResult,
        *,
        turn_id: str,
    ) -> None:
        return await self._execution_service.post_process(
            session_id=session_id,
            user_message=user_message,
            result=result,
            turn_id=turn_id,
        )

    @classmethod
    def result_payload(
        cls, result: HarnessRunResult, attachment_refs: list[AttachmentRef]
    ) -> dict[str, Any]:
        return ExecutionService.result_payload(result, attachment_refs)

    @staticmethod
    def deserialize_attachment_refs(
        payload: list[dict[str, Any]],
    ) -> list[AttachmentRef]:
        return ExecutionService.deserialize_attachment_refs(payload)

    @staticmethod
    def deserialize_messages(payload: list[dict[str, Any]]) -> list[Message]:
        return ExecutionService.deserialize_messages(payload)

    # ------------------------------------------------------------------
    # 附件处理（依赖 FileIntelligenceRuntime，保留在此）
    # ------------------------------------------------------------------

    async def process_attachment(
        self, attachment_id: str, message_id: str, session_id: str, run_id: str
    ) -> FileProcessResult:
        """在统一 Graph 内完成一个附件的解析和向量索引。"""
        context = AttachmentProcessContext(
            attachment_id=attachment_id,
            message_id=message_id,
            session_id=session_id,
            run_id=run_id,
        )
        return await StaticProcessorProxy(AttachmentProcessor(self)).execute(context)

    async def _process_attachment_once(
        self, attachment_id: str, message_id: str, session_id: str, run_id: str
    ) -> FileProcessResult:
        """Execute attachment parsing/indexing after Processor inspection."""
        if self._file_runtime is None:
            raise RuntimeError("file runtime is not configured")
        async with self._file_parse_semaphore:
            metadata = await self._file_runtime.parse_attachment(
                attachment_id, run_id=run_id
            )
        async with self._file_embedding_semaphore:
            indexed = await self._file_runtime.index_attachment(attachment_id)

        ready = await self._db.files.update_attachment(
            attachment_id,
            status="ready",
            error_message=None,
        )
        if ready is not None:
            await self._file_runtime.emit_attachment(ready, run_id=run_id)
        return {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "status": "ready",
            "error": None,
            "chunk_count": int(metadata.get("chunk_count", indexed.get("chunks", 0))),
        }

    # ------------------------------------------------------------------
    # 子 Agent 工具处理器
    # ------------------------------------------------------------------

    def delegation_tool_specs(self) -> list:
        """构建供组合根注册的 Agent 委派工具声明。"""
        from athena.core.tools.providers.agents import build_agent_tool_specs

        return build_agent_tool_specs(
            self._spawn_sub_agent_handler,
            self._spawn_parallel_handler,
            self._build_parallel_spawn_description(),
        )

    @staticmethod
    def _build_parallel_spawn_description() -> str:
        """构建 ``spawn_parallel_agents`` 工具描述。"""
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
        """``spawn_parallel_agents`` 工具的执行处理器。"""
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

        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.SUB_AGENT_SPAWNED,
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
        """``spawn_sub_agent`` 工具的执行处理器。"""
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
        """创建子 Agent 管理器实例。"""
        return SubAgentManager(
            llm=self._llm,
            tool_manager=self._tool_manager,
            db=self._db,
            event_publisher=self._events,
            compressor=self._compressor,
            memory_manager=self._memory_manager,
            settings=self._settings,
            main_run_id=main_run_id,
            agent_store=self._agent_store,
        )

    # ------------------------------------------------------------------
    # 记忆工作流
    # ------------------------------------------------------------------

    @property
    def memory_write_workflow(self) -> MemoryWriteWorkflow:
        if self._memory_write_workflow is None:
            self._memory_write_workflow = MemoryWriteWorkflow(
                extractor=self._fact_extractor,
                trigger=self._memory_trigger,
                resolver=self._memory_resolver,
                memory_manager=self._memory_manager,
            )
        return self._memory_write_workflow

    # ------------------------------------------------------------------
    # 运行时管理
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize runtime components."""
        logger.info("langgraph_runtime_initialized")

    def set_stop_signal(
        self, session_id: str, stop_signal: asyncio.Event | None
    ) -> None:
        self._session_stop_signals[session_id] = stop_signal

    def validate_state(self, state: AgentState) -> None:
        """Validate minimum required state fields exist."""
        if state.get("session_id") is None:
            raise ValueError("AgentState missing required field: session_id")
