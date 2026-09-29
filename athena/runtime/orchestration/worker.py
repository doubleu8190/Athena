"""Worker：按 TaskSpec 执行一个无依赖子任务并返回结构化结果。"""

from __future__ import annotations

import asyncio
from typing import Any

from athena.config.settings import Settings
from athena.contracts.ports import AgentStorePort, EventPublisherPort
from athena.contracts.statuses import AgentRunStatus
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessSettings
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.postgre.database import Database
from athena.contracts.orchestration import (
    AgentRole,
    TaskSpec,
    WorkerResult,
    WorkerResultStatus,
)
from athena.contracts.tool_policy import ToolPolicy
from athena.runtime.orchestration.structured_llm import StructuredLLMService
from athena.utils.id_generation import generate_sub_run_id
from athena.utils.logging import get_logger
from athena.utils.prompt_loader import get_prompt

logger = get_logger(__name__)


WORKER_SYSTEM_PROMPT = """你是 Athena 的执行 Worker。

把已完成的执行内容整理为结构化结果。output 字段必须匹配任务声明的
expected_output；如果执行失败，使用 error_code 和 error_message 说明原因。
不要扩展任务范围，也不要补充额外说明文本。"""


class WorkerExecutor:
    """执行单个任务的 Worker，复用 Harness 的 LLM/工具循环。"""

    def __init__(
        self,
        llm: Any,
        structured_llm: StructuredLLMService,
        tool_manager: UnifiedToolManager,
        db: Database,
        compressor: ContextCompressor,
        event_publisher: EventPublisherPort,
        agent_store: AgentStorePort,
        settings: Settings,
        root_run_id: str,
    ) -> None:
        """绑定 Worker 运行所需的依赖。

        参数：
            llm: 用于 Worker 对话的 LLM Provider。
            structured_llm: 用于最终结果结构化归一的调用服务。
            tool_manager: 当前 Runtime 的统一工具管理器。
            db: 数据库门面。
            compressor: 上下文压缩器，在每轮 LLM 调用前压缩消息列表。
            event_publisher: 应用事件发布器。
            agent_store: 用于创建和更新独立 Worker Run。
            settings: 全局配置。
            root_run_id: Worker 所属的 Root Run 标识。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._llm = llm
        self._structured_llm = structured_llm
        self._tool_manager = tool_manager
        self._db = db
        self._compressor = compressor
        self._event_publisher = event_publisher
        self._agent_store = agent_store
        self._settings = settings
        self._root_run_id = root_run_id
        self._structured_llm.validate_primary(WorkerResult)

    async def execute(
        self,
        task: TaskSpec,
        session_id: str,
        parent_run_id: str,
        root_run_id: str,
        index: int,
        attempt: int,
        stop_signal: asyncio.Event | None = None,
        run_id: str | None = None,
    ) -> WorkerResult:
        """执行任务并把最终文本转换为结构化结果。

        参数：
            task: 已通过 PlanMaterializer 校验的任务规格。
            session_id: Worker 所属用户会话。
            parent_run_id: 创建 Worker 的顶层 Agent Run 标识。
            root_run_id: Worker 所属的 Root Run 标识。
            index: 同一计划内的稳定序号。
            attempt: 从 1 开始的执行尝试序号。
            stop_signal: 可选停止事件。
            run_id: 可选预分配 Worker Run 标识；未提供时自动生成。

        返回值：
            WorkerResult: 已完成、失败或取消的结果。

        异常：
            ValueError: 工具策略无法从任务允许列表构建。
        """
        if stop_signal is not None and stop_signal.is_set():
            return self._cancelled(task, index)

        policy = ToolPolicy.for_worker(
            self._tool_manager.list_names(), task.allowed_tools
        )
        run_id = run_id or f"{generate_sub_run_id(self._root_run_id, index)}_{attempt}"
        await self._agent_store.create_worker_run(
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            attempt=attempt,
        )
        await self._agent_store.update_run_status(run_id, AgentRunStatus.RUNNING)

        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            compressor=self._compressor,
            event_publisher=self._event_publisher,
            harness_settings=HarnessSettings(
                max_turns_per_run=task.max_turns,
                tool_timeout=self._settings.tool_timeout,
            ),
        )
        result = await harness.run(
            messages=[{"role": "user", "content": task.objective}],
            session_id=session_id,
            system_prompt=get_prompt("sub_agent"),
            run_id=run_id,
            parent_run_id=parent_run_id,
            tool_names=sorted(policy.allowed_tools),
            stop_signal=stop_signal,
            agent_role=AgentRole.WORKER.value,
            plan_id=task.plan_id,
            task_id=task.task_id,
            depth=1,
        )

        if result.interrupted:
            await self._agent_store.update_run_status(run_id, AgentRunStatus.CANCELLED)
            return WorkerResult(
                plan_id=task.plan_id,
                task_id=task.task_id,
                run_id=run_id,
                status=WorkerResultStatus.CANCELLED,
                raw_text=result.content,
                turn_count=result.turn_count,
                tool_calls=result.tool_results,
                error_code="worker_cancelled",
                error_message="worker cancelled",
            )

        if result.error:
            await self._agent_store.update_run_status(
                run_id, AgentRunStatus.FAILED, result.error
            )
            return WorkerResult(
                plan_id=task.plan_id,
                task_id=task.task_id,
                run_id=run_id,
                status=WorkerResultStatus.FAILED,
                raw_text=result.content,
                turn_count=result.turn_count,
                tool_calls=result.tool_results,
                error_code="worker_failed",
                error_message=result.error,
            )

        await self._agent_store.update_run_status(run_id, AgentRunStatus.COMPLETED)
        structured = await self._structured_llm.generate(
            WorkerResult,
            WORKER_SYSTEM_PROMPT,
            f"任务ID: {task.task_id}\n任务目标: {task.objective}\n\n执行结果:\n{result.content}",
        )
        return structured.model_copy(
            update={
                "plan_id": task.plan_id,
                "task_id": task.task_id,
                "run_id": run_id,
                "raw_text": result.content,
                "turn_count": result.turn_count,
                "tool_calls": result.tool_results,
            }
        )

    @staticmethod
    def _cancelled(task: TaskSpec, index: int) -> WorkerResult:
        return WorkerResult(
            plan_id=task.plan_id,
            task_id=task.task_id,
            run_id=f"{task.plan_id}:{index}:cancelled",
            status=WorkerResultStatus.CANCELLED,
            error_code="worker_cancelled",
            error_message="worker cancelled before start",
        )
