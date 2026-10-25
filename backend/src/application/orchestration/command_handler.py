"""校验 Root 编排命令并交给持久化边界执行。"""

from __future__ import annotations

from collections.abc import Iterable

from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationCommandRepository,
    OrchestrationCommandType,
    Plan,
    PlanValidator,
    ReviewAction,
    ReviewDecision,
    Task,
)
from .protocols import RootPlanCommandPort


class OrchestrationCommandHandler(RootPlanCommandPort):
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
        *,
        legal_tools: Iterable[str] | None = None,
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
        self._validator.validate(plan, tasks, legal_tools=legal_tools)
        return await self._repository.submit_plan(command, plan, tasks)

    async def apply_review_decision(
        self,
        command: OrchestrationCommand,
        decision: ReviewDecision,
    ) -> CommandReceipt:
        """Bind a Root decision to the command type and reviewed identities."""
        expected_type = {
            ReviewAction.CONTINUE: OrchestrationCommandType.CONTINUE_PLAN,
            ReviewAction.REPLAN: OrchestrationCommandType.REVISE_PLAN,
            ReviewAction.FINISH: OrchestrationCommandType.FINISH_PLAN,
        }[decision.action]
        if command.command_type != expected_type:
            raise ValueError("review_action_command_type_mismatch")
        if command.plan_version is None or command.plan_version < 1:
            raise ValueError("review_command_plan_version_required")
        if not command.round_id:
            raise ValueError("review_command_round_id_required")
        if (
            decision.action == ReviewAction.REPLAN
            and decision.revision is not None
            and decision.revision.base_version != command.plan_version
        ):
            raise ValueError("review_command_revision_version_mismatch")
        return await self._repository.apply_review_decision(command, decision)

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None:
        """Recover an already committed decision before retrying the Root LLM."""
        return await self._repository.load_review_decision(
            scope_key=scope_key,
            idempotency_key=idempotency_key,
            plan_id=plan_id,
            plan_version=plan_version,
            round_id=round_id,
        )


__all__ = ["OrchestrationCommandHandler"]
