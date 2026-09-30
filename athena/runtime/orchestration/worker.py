"""Worker：按 TaskSpec 执行一个无依赖子任务并返回结构化结果。"""

from __future__ import annotations

import asyncio

from athena.contracts.ports import AgentStorePort, EventPublisherPort
from athena.contracts.statuses import AgentRunStatus
from athena.contracts.orchestration import (
    TaskSpec,
    WorkerResult,
    WorkerResultStatus,
)
from athena.contracts.tool_policy import ToolPolicy
from athena.runtime.execution_loop.runner import run_agent_loop
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
    """执行单个任务的 Worker，复用 LangGraph execution-loop。"""

    def __init__(
        self,
        graph_runtime: object,
        structured_llm: StructuredLLMService,
        event_publisher: EventPublisherPort,
        agent_store: AgentStorePort,
        root_run_id: str,
    ) -> None:
        """绑定 Worker 运行所需的依赖。

        参数：
            graph_runtime: 统一的 LangGraph Agent loop 运行时。
            structured_llm: 用于最终结果结构化归一的调用服务。
            event_publisher: 应用事件发布器。
            agent_store: 用于创建和更新独立 Worker Run。
            root_run_id: Worker 所属的 Root Run 标识。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._graph_runtime = graph_runtime
        self._structured_llm = structured_llm
        self._tool_manager = graph_runtime._tool_manager
        self._event_publisher = event_publisher
        self._agent_store = agent_store
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

        result = await run_agent_loop(
            self._graph_runtime,
            session_id=session_id,
            run_id=run_id,
            user_message=task.objective,
            system_prompt=get_prompt("sub_agent"),
            tool_names=sorted(policy.allowed_tools),
            max_turns=task.max_turns,
            parent_run_id=parent_run_id,
            stop_signal=stop_signal,
            plan_id=task.plan_id,
            task_id=task.task_id,
        )

        result_content = str(result.get("content") or "")
        result_error = result.get("error")
        result_turn_count = int(result.get("turn_count", 0))
        result_tool_calls = list(result.get("tool_results") or [])
        if result.get("interrupted", False):
            await self._agent_store.update_run_status(run_id, AgentRunStatus.CANCELLED)
            return WorkerResult(
                plan_id=task.plan_id,
                task_id=task.task_id,
                run_id=run_id,
                status=WorkerResultStatus.CANCELLED,
                raw_text=result_content,
                turn_count=result_turn_count,
                tool_calls=result_tool_calls,
                error_code="worker_cancelled",
                error_message="worker cancelled",
            )

        if result_error:
            await self._agent_store.update_run_status(
                run_id, AgentRunStatus.FAILED, str(result_error)
            )
            return WorkerResult(
                plan_id=task.plan_id,
                task_id=task.task_id,
                run_id=run_id,
                status=WorkerResultStatus.FAILED,
                raw_text=result_content,
                turn_count=result_turn_count,
                tool_calls=result_tool_calls,
                error_code="worker_failed",
                error_message=str(result_error),
            )

        await self._agent_store.update_run_status(run_id, AgentRunStatus.COMPLETED)
        structured = await self._structured_llm.generate(
            WorkerResult,
            WORKER_SYSTEM_PROMPT,
            f"任务ID: {task.task_id}\n任务目标: {task.objective}\n\n执行结果:\n{result_content}",
        )
        return structured.model_copy(
            update={
                "plan_id": task.plan_id,
                "task_id": task.task_id,
                "run_id": run_id,
                "raw_text": result_content,
                "turn_count": result_turn_count,
                "tool_calls": result_tool_calls,
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
