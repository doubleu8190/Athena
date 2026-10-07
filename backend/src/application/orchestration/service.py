"""计划校验、持久化和调度入口。"""

from __future__ import annotations

from collections.abc import Iterable

from domain.orchestration import (
    Plan,
    PlanDAG,
    PlanRepository,
    PlanScheduler,
    PlanValidator,
    Task,
)


class PlanNotFoundError(LookupError):
    """请求的计划不存在。"""


class OrchestrationService:
    """编排用例的 application 边界。"""

    def __init__(
        self,
        repository: PlanRepository,
        *,
        validator: PlanValidator | None = None,
        scheduler: PlanScheduler | None = None,
    ) -> None:
        self._repository = repository
        self._validator = validator or PlanValidator()
        self._scheduler = scheduler

    async def create_plan(self, plan: Plan, tasks: Iterable[Task]) -> Plan:
        values = list(tasks)
        self._validator.validate(plan, values)
        return await self._repository.create(plan, values)

    async def get_plan(self, plan_id: str) -> tuple[Plan, list[Task]]:
        value = await self._repository.get(plan_id)
        if value is None:
            raise PlanNotFoundError(plan_id)
        return value

    async def start_ready_tasks(self, plan_id: str) -> dict[str, object]:
        if self._scheduler is None:
            raise RuntimeError("plan scheduler is not configured")
        await self.get_plan(plan_id)
        return await self._scheduler.start_ready_tasks(plan_id)

    async def ready_tasks(self, plan_id: str, states: dict[str, str]) -> list[str]:
        plan, _ = await self.get_plan(plan_id)
        return PlanDAG(plan).ready_tasks(states)


__all__ = ["OrchestrationService", "PlanNotFoundError"]
