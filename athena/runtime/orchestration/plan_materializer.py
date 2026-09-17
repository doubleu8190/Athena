"""将顶层 Agent LLM 提交的计划校验、规范化并持久化。"""

from __future__ import annotations

from athena.contracts.events import EventType
from athena.infrastructure.sqlite.database import Database
from athena.runtime.orchestration.contracts import (
    ExecutionPlan,
    PLAN_SUBMISSION_TOOL_NAME,
    PlanSubmission,
    TaskSpec,
)
from athena.runtime.orchestration.events import OrchestrationEventPublisher
from athena.runtime.orchestration.policies import DELEGATION_TOOL_NAMES
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class PlanMaterializer:
    """只负责处理顶层 Agent LLM 已经决定提交的执行计划。"""

    def __init__(self, db: Database, events: OrchestrationEventPublisher) -> None:
        self._db = db
        self._events = events

    async def materialize_submission(
        self,
        session_id: str,
        root_run_id: str,
        user_goal: str,
        submission: dict[str, object],
        available_tools: list[str],
    ) -> ExecutionPlan:
        """校验控制调用参数，并建立可执行的持久化计划。"""
        draft = PlanSubmission.model_validate(submission)
        plan_id = draft.plan_id.strip()
        goal = draft.goal.strip() or user_goal.strip()
        normalized_tasks = [
            TaskSpec(
                task_id=task.task_id,
                plan_id=plan_id,
                title=task.title,
                objective=task.objective,
                input_context=task.input_context,
                expected_output=task.expected_output,
                allowed_tools=[
                    name
                    for name in task.allowed_tools
                    if name in available_tools
                    and name not in DELEGATION_TOOL_NAMES
                    and name != PLAN_SUBMISSION_TOOL_NAME
                ],
                depends_on=[],
                max_turns=task.max_turns,
                timeout_seconds=task.timeout_seconds,
                retry_limit=task.retry_limit,
            )
            for task in draft.tasks
        ]
        plan = ExecutionPlan(
            plan_id=plan_id,
            root_run_id=root_run_id,
            goal=goal,
            tasks=normalized_tasks,
            max_parallelism=draft.max_parallelism,
        )
        await self._db.orchestration.create_plan(session_id, plan)
        await self._events.publish_plan(
            EventType.PLAN_CREATED,
            session_id=session_id,
            plan_id=plan.plan_id,
            run_id=root_run_id,
            payload={"task_count": len(plan.tasks), "goal": plan.goal},
        )
        logger.info(
            "execution_plan_materialized",
            session_id=session_id,
            root_run_id=root_run_id,
            plan_id=plan.plan_id,
            task_count=len(plan.tasks),
            source=PLAN_SUBMISSION_TOOL_NAME,
        )
        return plan
