"""编排快照和不可变 Plan Version 查询用例。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class OrchestrationReadPort(Protocol):
    """读取持久化编排事实的端口。"""

    async def get_run_orchestration_snapshot(
        self, run_id: str
    ) -> Mapping[str, object] | None: ...

    async def get_plan_version(
        self, plan_id: str, version: int
    ) -> Mapping[str, object] | None: ...


class OrchestrationQueryService:
    """通过只读仓储提供可恢复的 Run 编排视图。"""

    def __init__(self, repository: OrchestrationReadPort) -> None:
        self._repository = repository

    async def run_snapshot(self, run_id: str) -> Mapping[str, object] | None:
        return await self._repository.get_run_orchestration_snapshot(run_id)

    async def plan_version(
        self, plan_id: str, version: int
    ) -> Mapping[str, object] | None:
        return await self._repository.get_plan_version(plan_id, version)


__all__ = ["OrchestrationQueryService", "OrchestrationReadPort"]
