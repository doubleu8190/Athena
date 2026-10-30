"""编排快照和不可变 Plan Version 查询用例。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class OrchestrationReadPort(Protocol):
    """读取持久化编排事实的端口。"""

    async def get_run_orchestration_snapshot(
        self, run_id: str
    ) -> Mapping[str, object] | None:
        """读取指定 Run 的编排快照。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Mapping[str, object] | None：Run 的编排状态和关联计划快照；Run 不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def get_plan_version(
        self, plan_id: str, version: int
    ) -> Mapping[str, object] | None:
        """读取指定计划的不可变版本快照。

        参数：
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `version`（int）：目标计划版本号，用于定位不可变计划快照；约束：无额外运行时限制。

        返回：
        - Mapping[str, object] | None：指定计划版本的任务和依赖快照；版本不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class OrchestrationQueryService:
    """通过只读仓储提供可恢复的 Run 编排视图。"""

    def __init__(self, repository: OrchestrationReadPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（OrchestrationReadPort）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository

    async def run_snapshot(self, run_id: str) -> Mapping[str, object] | None:
        """执行快照。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Mapping[str, object] | None：Run 编排快照映射；对应运行不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._repository.get_run_orchestration_snapshot(run_id)

    async def plan_version(
        self, plan_id: str, version: int
    ) -> Mapping[str, object] | None:
        """生成计划版本。

        参数：
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `version`（int）：目标计划版本号，用于定位不可变计划快照；约束：无额外运行时限制。

        返回：
        - Mapping[str, object] | None：计划版本快照映射；对应版本不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._repository.get_plan_version(plan_id, version)


__all__ = ["OrchestrationQueryService", "OrchestrationReadPort"]
