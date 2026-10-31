"""校验 Root 编排命令并交给持久化边界执行。"""

from __future__ import annotations

from collections.abc import Iterable

from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationCommandRepository,
    OrchestrationCommandType,
    Plan,
    ReviewAction,
    ReviewDecision,
    Task,
)
from .protocols import RootPlanCommandPort


class OrchestrationCommandHandler(RootPlanCommandPort):
    """处理 Root 仍使用的非 Plan 命令，并显式隔离旧 Plan 提交入口。"""

    def __init__(
        self,
        repository: OrchestrationCommandRepository,
    ) -> None:
        """保存非 Plan 命令使用的编排仓储。

        参数：
        - `repository`（OrchestrationCommandRepository）：处理未退役 Root 命令的持久化端口。

        返回：
        - None：操作完成后不提供返回数据。

        异常：无预期业务异常。
        """
        self._repository = repository

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
        *,
        legal_tools: Iterable[str] | None = None,
    ) -> CommandReceipt:
        """旧 DAG 提交入口在 S2 停用，防止请求写入已退役模型。

        参数：
        - `command`（OrchestrationCommand）：必须是首版 SubmitPlan，且绑定目标 Run；约束：须通过方法内校验；失败条件见下方异常说明。
        - `plan`（Plan）：与命令关联的初始 Plan；约束：须通过方法内校验；失败条件见下方异常说明。
        - `tasks`（list[Task]）：计划内所有 Task 定义；约束：无额外运行时限制。
        - `legal_tools`（Iterable[str] | None）：计划任务允许调用的工具名称集合；None 表示由上游规则确定；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - CommandReceipt：首轮成员和稳定命令 ID 构成的回执。

        异常：
        - RuntimeError：S3/S7 接通远程 Worker 与 Root 多轮流程前，旧入口一律返回 execution_not_available_during_worker_s2。
        """
        raise RuntimeError("execution_not_available_during_worker_s2")

    async def apply_review_decision(
        self,
        command: OrchestrationCommand,
        decision: ReviewDecision,
    ) -> CommandReceipt:
        """将审查决策绑定到命令类型和 Round 身份，并提交幂等命令。

        参数：
        - `command`（OrchestrationCommand）：领域命令，携带幂等身份和目标对象；约束：须通过方法内校验；失败条件见下方异常说明。
        - `decision`（ReviewDecision）：经过校验的决策内容，用于驱动后续状态转换；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - CommandReceipt：命令处理结果及其幂等标识、持久化状态。

        异常：
        - ValueError：校验条件 `command.command_type != expected_type` 成立时（异常说明：review_action_command_type_mismatch）。
        - ValueError：校验条件 `command.plan_version is None or command.plan_version < 1` 成立时（异常说明：review_command_plan_version_required）。
        - ValueError：校验条件 `not command.round_id` 成立时（异常说明：review_command_round_id_required）。
        - ValueError：校验条件 `decision.action == ReviewAction.REPLAN and decision.revision is not None and (decision.revision.base_version != command.plan_version)` 成立时（异常说明：review_command_revision_version_mismatch）。
        - 未列出的依赖异常按当前实现传播。
        """
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
        """按幂等身份读取已提交的审查决策，供模型重试前复用。

        参数：
        - `scope_key`（str）：限定幂等命令作用范围的稳定键；约束：无额外运行时限制。
        - `idempotency_key`（str）：同一业务命令重试时保持不变的幂等键；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。

        返回：
        - ReviewDecision | None：Root 对已完成 Round 选择的继续、修订或结束动作。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await self._repository.load_review_decision(
            scope_key=scope_key,
            idempotency_key=idempotency_key,
            plan_id=plan_id,
            plan_version=plan_version,
            round_id=round_id,
        )


__all__ = ["OrchestrationCommandHandler"]
