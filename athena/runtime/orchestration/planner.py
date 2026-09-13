"""Planner：把用户目标转换为受约束的 V1 执行计划或直接回答。"""

from __future__ import annotations

from athena.infrastructure.sqlite.database import Database
from athena.runtime.orchestration.contracts import (
    ExecutionPlan,
    PlannerDecisionDraft,
    PlanningDecision,
    TaskSpec,
)
from athena.runtime.orchestration.structured_llm import StructuredLLMService
from athena.runtime.orchestration.events import OrchestrationEventPublisher
from athena.contracts.events import EventType
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

logger = get_logger(__name__)


PLANNER_SYSTEM_PROMPT = get_prompt("planner")


class Planner:
    """通过原生 Structured Output 生成规划决策和执行计划。"""

    def __init__(
        self,
        llm: StructuredLLMService,
        db: Database,
        events: OrchestrationEventPublisher,
    ) -> None:
        """绑定结构化调用服务和任务账本。

        参数：
            llm: 统一的 Structured Output 调用服务。
            db: 数据库门面，计划会写入 ``db.orchestration``。
            events: 编排生命周期事件发布器。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._llm = llm
        self._db = db
        self._events = events
        self._llm.validate_primary(PlannerDecisionDraft)

    async def decide(
        self,
        session_id: str,
        root_run_id: str,
        goal: str,
        available_tools: list[str],
        context: str = "",
    ) -> PlanningDecision:
        """生成规划决策，并在落库前把运行标识绑定到计划。

        参数：
            session_id: 用户会话标识。
            root_run_id: 顶层运行标识。
            goal: 用户请求的完整目标。
            available_tools: Planner 可分配给 Worker 的工具名。
            context: 已检索到的记忆或附件上下文。

        返回值：
            PlanningDecision: 直接回答决策或绑定运行标识的执行计划。

        异常：
            NotImplementedError: 编排模型不支持结构化输出。
            ValueError: 模型输出无法解析或违反 V1 约束。
        """
        user_message = goal if not context else f"{goal}\n\n[上下文]\n{context}"
        draft = await self._llm.generate(
            PlannerDecisionDraft,
            PLANNER_SYSTEM_PROMPT,
            user_message,
        )

        if draft.mode == "direct_answer":
            return PlanningDecision(
                mode="direct_answer", direct_answer=draft.direct_answer
            )

        normalized_tasks = [
            TaskSpec(
                task_id=task.task_id,
                plan_id=draft.plan_id,
                title=task.title,
                objective=task.objective,
                input_context=task.input_context,
                expected_output=task.expected_output,
                allowed_tools=[
                    name for name in task.allowed_tools if name in available_tools
                ],
                depends_on=[],
                max_turns=task.max_turns,
                timeout_seconds=task.timeout_seconds,
                retry_limit=task.retry_limit,
            )
            for task in draft.tasks
        ]
        plan = ExecutionPlan(
            plan_id=draft.plan_id,
            root_run_id=root_run_id,
            goal=draft.goal,
            tasks=normalized_tasks,
            aggregation_strategy=draft.aggregation_strategy,
            max_parallelism=draft.max_parallelism,
        )
        plan = plan.model_copy(update={"tasks": normalized_tasks})
        await self._db.orchestration.create_plan(session_id, plan)
        await self._events.publish_plan(
            EventType.PLAN_CREATED,
            session_id=session_id,
            plan_id=plan.plan_id,
            run_id=root_run_id,
            payload={"task_count": len(plan.tasks), "goal": plan.goal},
        )
        logger.info(
            "execution_plan_created",
            session_id=session_id,
            root_run_id=root_run_id,
            plan_id=plan.plan_id,
            task_count=len(plan.tasks),
        )
        return PlanningDecision(mode="execute_plan", plan=plan)

    async def plan(
        self,
        session_id: str,
        root_run_id: str,
        goal: str,
        available_tools: list[str],
        context: str = "",
    ) -> ExecutionPlan:
        """生成并持久化计划，保留给显式计划执行调用方。

        参数：
            session_id: 用户会话标识。
            root_run_id: 顶层运行标识。
            goal: 用户请求的完整目标。
            available_tools: Planner 可分配给 Worker 的工具名。
            context: 已检索到的记忆或附件上下文。

        返回值：
            ExecutionPlan: 已校验并持久化的计划。

        异常：
            NotImplementedError: 编排模型不支持结构化输出。
            ValueError: 模型输出无法解析或违反 V1 约束。
        """
        decision = await self.decide(
            session_id=session_id,
            root_run_id=root_run_id,
            goal=goal,
            available_tools=available_tools,
            context=context,
        )
        if decision.mode == "direct_answer" or decision.plan is None:
            raise ValueError("planner did not produce an execution plan")
        return decision.plan
