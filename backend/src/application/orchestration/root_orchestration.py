"""Root 通过应用端口读取能力和事实并提交逐轮 Plan。"""

from __future__ import annotations

from domain.leases import LeaseGrant
from domain.orchestration import RootPlanFacts, WorkerPlanReceipt, WorkerPlanSubmission
from domain.orchestration.ports import WorkerOrchestrationRepository

from .root_decision import RootWorkerCapability


class RootOrchestrationService:
    """封装 Root 可用的 Orchestrator 应用操作。"""

    def __init__(self, repository: WorkerOrchestrationRepository) -> None:
        """保存逐轮编排仓储。

        参数：`repository` 提供能力读取、Plan 提交和完成事实读取。

        返回：无。

        异常：不主动抛出业务异常。
        """
        self._repository = repository

    async def list_authorized_capabilities(
        self, run_id: str
    ) -> tuple[RootWorkerCapability, ...]:
        """读取授权给 Root 的不可变能力契约目录。

        参数：`run_id` 限定能力查询的 Run 身份，必须非空。

        返回：按契约身份排序且不包含实例路由、容量或超时的能力契约。

        异常：仓储读取异常原样传播。
        """
        capabilities = await self._repository.list_authorized_capabilities(run_id)
        return tuple(
            RootWorkerCapability(
                contract_id=item.contract_id,
                contract_revision=item.revision,
                can_do=item.can_do,
                cannot_do=item.cannot_do,
                input_schema=item.input_schema,
                output_schema=item.output_schema,
            )
            for item in capabilities
        )

    async def submit_plan(
        self, submission: WorkerPlanSubmission, *, lease: LeaseGrant
    ) -> WorkerPlanReceipt:
        """通过 Orchestrator 应用仓储提交单轮独立任务。

        参数：`submission` 是已冻结身份和输入的 WorkerPlanSubmission。

        返回：首次提交或精确重放的稳定 Plan 回执。

        异常：仓储校验和持久化异常原样传播。
        """
        return await self._repository.submit_root_plan(submission, lease=lease)

    async def read_completed_plan(
        self, run_id: str, plan_id: str, root_wait_generation: int
    ) -> RootPlanFacts | None:
        """读取与当前 Root 等待代次匹配的完整 Plan 终态事实。

        参数：`run_id`、`plan_id` 和 `root_wait_generation` 绑定单轮等待身份。

        返回：Plan 尚未全部终结或身份已过期时返回 None，否则返回包含所有任务和尝试的快照。

        异常：仓储读取异常原样传播。
        """
        return await self._repository.read_completed_plan(
            run_id, plan_id, root_wait_generation
        )


__all__ = ["RootOrchestrationService"]
