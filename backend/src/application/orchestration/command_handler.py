"""校验 Root 编排命令并交给持久化边界执行。"""

from __future__ import annotations

from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationCommandRepository,
    OrchestrationCommandType,
    Plan,
    PlanValidator,
    Task,
)


class OrchestrationCommandHandler:
    """提供经过 DAG 校验的 SubmitPlan 用例。"""

    def __init__(
        self,
        repository: OrchestrationCommandRepository,
        *,
        validator: PlanValidator | None = None,
    ) -> None:
        """保存编排命令仓储和确定性 Plan 校验器。

        Args:
            repository: 在一个事务中接受命令并保存业务状态的仓储。
            validator: DAG、Task 和工具集合校验器。
        """
        self._repository = repository
        self._validator = validator or PlanValidator()

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
    ) -> CommandReceipt:
        """验证并持久化首版 Plan。

        Args:
            command: 必须是首版 SubmitPlan，且绑定目标 Run。
            plan: 与命令关联的初始 Plan。
            tasks: 计划内所有 Task 定义。

        Returns:
            首轮成员和稳定命令 ID 构成的回执。

        Raises:
            ValueError: 命令类型或 Plan 身份不匹配。
            PlanValidationError: Plan 的 DAG、任务或工具校验失败。
        """
        if command.command_type != OrchestrationCommandType.SUBMIT_PLAN:
            raise ValueError("submit_plan requires a SubmitPlan command")
        if command.plan_id != plan.plan_id:
            raise ValueError("command and plan IDs do not match")
        if command.correlation_id != plan.run_id:
            raise ValueError("command correlation_id must equal plan.run_id")
        self._validator.validate(plan, tasks)
        return await self._repository.submit_plan(command, plan, tasks)


__all__ = ["OrchestrationCommandHandler"]
