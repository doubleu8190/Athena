"""子 Agent 生命周期管理。

包含 ``SubAgentResult`` 数据类和 ``SubAgentManager`` 管理器，
支持串行 / 并行子任务派生，与父级共享工具集与审批队列。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessRunResult, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import AgentStorePort, EventPublisherPort
from athena.contracts.statuses import AgentRunStatus
from athena.infrastructure.sqlite.database import Database
from athena.utils.ids import generate_sub_run_id
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt
from athena.runtime.orchestration import AgentRole, ToolPolicy

logger = get_logger(__name__)


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
        settings: Settings,
        main_run_id: str,
        agent_store: AgentStorePort,
    ) -> None:
        """

        参数：
            llm (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            tool_manager (UnifiedToolManager): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            db (数据库): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            event_publisher: 应用事件发布器。
            compressor (ContextCompressor): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
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
        self._settings = settings
        self._main_run_id = main_run_id
        self._agent_store = agent_store
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
        attempt: int = 1,
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
            SubAgentResult: 包含子 Agent 输出文本、轮次计数、工具调用结果
            和错误信息（如有）的结果对象。

        异常：
            SubAgentError: 子 Agent 初始化或执行失败时抛出。
        """
        async with self._lock:
            self._sub_counter += 1
            index = self._sub_counter
        sub_run_id = (
            generate_sub_run_id(self._main_run_id, index)
            if self._main_run_id
            else generate_sub_run_id("sub", index)
        )
        sub_run_id = f"{sub_run_id}_{attempt}"
        await self._agent_store.create_worker_run(
            run_id=sub_run_id,
            session_id=session_id,
            parent_run_id=self._main_run_id,
            root_run_id=self._main_run_id,
            plan_id=None,
            task_id=None,
            attempt=attempt,
        )
        await self._agent_store.update_run_status(
            sub_run_id, AgentRunStatus.RUNNING
        )

        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.SUB_AGENT_STARTED,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=sub_run_id,
                payload={
                    "task": task,
                    "sub_run_id": sub_run_id,
                    "parent_run_id": self._main_run_id,
                },
            )
        )

        worker_policy = ToolPolicy.for_worker(
            self._tool_manager.list_names(), allowed_tools
        )
        sub_harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            event_publisher=self._events,
            compressor=self._compressor,
            harness_settings=HarnessSettings(
                max_turns_per_run=max_turns,
                tool_timeout=self._settings.tool_timeout,
                approval_timeout=self._settings.approval_timeout,
            ),
        )

        try:
            result = await sub_harness.run(
                messages=[{"role": "user", "content": task}],
                session_id=session_id,
                system_prompt=get_prompt("sub_agent"),
                run_id=sub_run_id,
                parent_run_id=self._main_run_id,
                tool_names=sorted(worker_policy.allowed_tools),
                stop_signal=stop_signal,
                agent_role=AgentRole.WORKER.value,
                depth=1,
            )
            sub_result = SubAgentResult(
                task=task,
                content=result.content,
                turn_count=result.turn_count,
                tool_results=result.tool_results,
                error=result.error,
                run_id=sub_run_id,
            )
            await self._agent_store.update_run_status(
                sub_run_id,
                AgentRunStatus.CANCELLED
                if result.interrupted
                else (
                    AgentRunStatus.FAILED
                    if result.error
                    else AgentRunStatus.COMPLETED
                ),
                result.error,
            )
            await self._events.publish(
                ApplicationEvent(
                    event_type=EventType.SUB_AGENT_COMPLETE,
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
            await self._agent_store.update_run_status(
                sub_run_id, AgentRunStatus.FAILED, str(e)
            )
            await self._events.publish(
                ApplicationEvent(
                    event_type=EventType.SUB_AGENT_FAILED,
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=sub_run_id,
                    payload={"task": task, "sub_run_id": sub_run_id, "error": str(e)},
                )
            )
            return SubAgentResult(
                task=task, content="", error=str(e), run_id=sub_run_id
            )

    async def spawn_parallel(
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
