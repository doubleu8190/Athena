"""Worker 注册实例健康状态和能力快照的 PostgreSQL 适配器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.orchestration.worker_entities import WorkerInstanceStatus
from domain.orchestration.worker_registry import (
    WorkerAvailableInstance,
    WorkerHealthProbeResult,
    WorkerHealthProbeTarget,
    WorkerHealthTransition,
)
from ..models import (
    OrchestratorExecutionModel,
    OrchestratorWorkerCommandModel,
    WorkerContractModel,
    WorkerInstanceModel,
)


class PostgresWorkerRegistryRepository:
    """以注册时间戳 CAS 应用健康结果并读取可分配能力。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化注册仓储。

        参数：
        - `session_factory`（async_sessionmaker[AsyncSession]）：提供事务会话的异步工厂。
        - `clock`（Callable[[], datetime] | None）：可选测试时钟；生产使用 PostgreSQL 时钟。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._session_factory = session_factory
        self._clock = clock

    async def list_health_probe_targets(self) -> list[WorkerHealthProbeTarget]:
        """返回所有尚未禁用的实例快照，供健康监视器逐个探测。

        参数：无。

        返回：以实例 ID 排序的探测目标列表。

        异常：数据库查询错误原样传播。
        """
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(WorkerInstanceModel)
                    .where(WorkerInstanceModel.status != WorkerInstanceStatus.DISABLED.value)
                    .order_by(WorkerInstanceModel.instance_id)
                )
            ).all()
            return [
                WorkerHealthProbeTarget(
                    instance_id=row.instance_id,
                    instance_key=row.instance_key,
                    service_principal=row.service_principal,
                    base_url=row.base_url,
                    registration_updated_at=row.updated_at,
                )
                for row in rows
            ]

    async def record_health_success(
        self,
        target: WorkerHealthProbeTarget,
        result: WorkerHealthProbeResult,
        *,
        recovery_threshold: int,
    ) -> WorkerHealthTransition | None:
        """以注册快照 CAS 记录有效探测，并达到阈值后恢复接单。

        参数：
        - `target`（WorkerHealthProbeTarget）：发起 HTTP 探测时捕获的实例快照。
        - `result`（WorkerHealthProbeResult）：身份匹配且已通过协议校验的响应。
        - `recovery_threshold`（int）：连续 ready 探测后恢复可接单状态的阈值。

        返回：快照仍有效且事务更新成功时的健康状态，否则返回 `None`。

        异常：ValueError 表示恢复阈值小于 1。
        """
        if recovery_threshold < 1:
            raise ValueError("worker_health_recovery_threshold_invalid")
        async with self._session_factory() as session, session.begin():
            row = await self._locked_snapshot(session, target)
            if row is None:
                return None
            checked_at = await self._now(session)
            ready_response = result.status == "ready"
            was_available_for_health = (
                row.status == WorkerInstanceStatus.READY.value
                and row.health_identity_valid
            )
            row.consecutive_health_failures = 0
            if not row.health_identity_valid:
                row.consecutive_health_successes = 0
            row.health_identity_valid = True
            if ready_response:
                if was_available_for_health:
                    row.status = WorkerInstanceStatus.READY.value
                    row.accepting_tasks = result.accepting_tasks
                else:
                    row.consecutive_health_successes += 1
                    if row.consecutive_health_successes >= recovery_threshold:
                        row.status = WorkerInstanceStatus.READY.value
                        row.accepting_tasks = result.accepting_tasks
                    else:
                        row.status = WorkerInstanceStatus.NOT_READY.value
                        row.accepting_tasks = False
            else:
                row.consecutive_health_successes = 0
                row.status = WorkerInstanceStatus.NOT_READY.value
                row.accepting_tasks = False
            row.last_health_check_at = checked_at
            row.updated_at = checked_at
            return self._transition(row)

    async def record_health_failure(
        self,
        target: WorkerHealthProbeTarget,
        *,
        failure_threshold: int,
    ) -> WorkerHealthTransition | None:
        """记录 HTTP、TLS 或 Schema 探测失败并在阈值后摘除实例。

        参数：
        - `target`（WorkerHealthProbeTarget）：探测前读取的实例注册快照。
        - `failure_threshold`（int）：连续失败到达该次数时移出可用列表。

        返回：CAS 更新成功时的健康状态；若实例在探测期间已重新注册则返回 `None`。

        异常：ValueError 表示失败阈值小于 1。
        """
        if failure_threshold < 1:
            raise ValueError("worker_health_failure_threshold_invalid")
        async with self._session_factory() as session, session.begin():
            row = await self._locked_snapshot(session, target)
            if row is None:
                return None
            checked_at = await self._now(session)
            row.consecutive_health_failures += 1
            row.consecutive_health_successes = 0
            if row.consecutive_health_failures >= failure_threshold:
                row.status = WorkerInstanceStatus.NOT_READY.value
                row.accepting_tasks = False
            row.last_health_check_at = checked_at
            row.updated_at = checked_at
            return self._transition(row)

    async def record_identity_mismatch(
        self, target: WorkerHealthProbeTarget
    ) -> WorkerHealthTransition | None:
        """禁止向回显错误实例键的地址继续派发且保留连续计数。

        参数：
        - `target`（WorkerHealthProbeTarget）：唯一可信的预期实例路由。

        返回：CAS 更新成功时 not_ready 状态；注册快照过期时返回 `None`。

        异常：数据库错误原样传播。
        """
        async with self._session_factory() as session, session.begin():
            row = await self._locked_snapshot(session, target)
            if row is None:
                return None
            checked_at = await self._now(session)
            row.status = WorkerInstanceStatus.NOT_READY.value
            row.accepting_tasks = False
            row.health_identity_valid = False
            row.last_health_check_at = checked_at
            row.updated_at = checked_at
            return self._transition(row)

    async def list_available_workers(self) -> list[WorkerAvailableInstance]:
        """读取健康、主动接单且保留原契约版本的 Worker 实例。

        参数：无。

        返回：按实例 ID 排序的能力快照，不返回认证 token 或网络探测资料。

        异常：数据库查询错误原样传播。
        """
        async with self._session_factory() as session:
            active_reservation_count = (
                select(func.count())
                .select_from(OrchestratorExecutionModel)
                .where(
                    OrchestratorExecutionModel.instance_id == WorkerInstanceModel.instance_id,
                    OrchestratorExecutionModel.capacity_reserved.is_(True),
                )
                .correlate(WorkerInstanceModel)
                .scalar_subquery()
            )
            rows = (
                await session.execute(
                    select(
                        WorkerInstanceModel,
                        WorkerContractModel,
                        active_reservation_count.label("current_concurrency"),
                    )
                    .join(
                        WorkerContractModel,
                        (WorkerContractModel.contract_id == WorkerInstanceModel.contract_id)
                        & (WorkerContractModel.revision == WorkerInstanceModel.contract_revision),
                    )
                    .where(
                        WorkerInstanceModel.status == WorkerInstanceStatus.READY.value,
                        WorkerInstanceModel.accepting_tasks.is_(True),
                    )
                    .order_by(WorkerInstanceModel.instance_id)
                )
            ).all()
            return [
                WorkerAvailableInstance(
                    instance_id=instance.instance_id,
                    instance_key=instance.instance_key,
                    contract_id=contract.contract_id,
                    contract_revision=contract.revision,
                    execution_timeout_seconds=instance.execution_timeout_seconds,
                    max_concurrency=instance.max_concurrency,
                    current_concurrency=int(current_concurrency),
                    available_concurrency=max(
                        0, instance.max_concurrency - int(current_concurrency)
                    ),
                    can_do=tuple(contract.can_do_json),
                    cannot_do=tuple(contract.cannot_do_json),
                    input_schema=contract.input_schema_json,
                    output_schema=contract.output_schema_json,
                )
                for instance, contract, current_concurrency in rows
                if int(current_concurrency) < instance.max_concurrency
            ]

    async def resolve_execution_instance(
        self, service_principal: str, execution_id: str
    ) -> str | None:
        """从当前 Execution 分配记录解析认证主体拥有的 Worker。

        参数：
        - `service_principal`（str）：认证服务主体。
        - `execution_id`（str）：结果提交的路径 Execution ID。

        返回：归属主体的 Worker 实例 ID；未分配或主体不匹配时返回 `None`。

        异常：数据库读取错误原样传播。
        """
        async with self._session_factory() as session:
            return await session.scalar(
                select(WorkerInstanceModel.instance_id)
                .join(
                    OrchestratorExecutionModel,
                    OrchestratorExecutionModel.instance_id == WorkerInstanceModel.instance_id,
                )
                .where(
                    OrchestratorExecutionModel.execution_id == execution_id,
                    WorkerInstanceModel.service_principal == service_principal,
                )
            )

    async def resolve_cancel_instance(
        self,
        service_principal: str,
        execution_id: str,
        cancel_command_id: str,
    ) -> str | None:
        """验证 StopConfirmation 引用的 CancelCommand 与认证实例完全匹配。

        参数：
        - `service_principal`（str）：认证服务主体。
        - `execution_id`（str）：请求路径中的 Execution ID。
        - `cancel_command_id`（str）：请求体引用的原 CancelCommand UUID。

        返回：命令所属实例 ID；身份、路径、命令类型任一不匹配时返回 `None`。

        异常：数据库读取错误原样传播。
        """
        async with self._session_factory() as session:
            return await session.scalar(
                select(WorkerInstanceModel.instance_id)
                .join(
                    OrchestratorWorkerCommandModel,
                    OrchestratorWorkerCommandModel.instance_id
                    == WorkerInstanceModel.instance_id,
                )
                .where(
                    OrchestratorWorkerCommandModel.command_id == cancel_command_id,
                    OrchestratorWorkerCommandModel.execution_id == execution_id,
                    OrchestratorWorkerCommandModel.command_type == "cancel",
                    WorkerInstanceModel.service_principal == service_principal,
                )
            )

    @staticmethod
    async def _locked_snapshot(
        session: AsyncSession, target: WorkerHealthProbeTarget
    ) -> WorkerInstanceModel | None:
        """仅锁定仍与探测起始快照一致的注册实例。

        参数：
        - `session`（AsyncSession）：当前事务会话。
        - `target`（WorkerHealthProbeTarget）：探测时读到的实例 ID、键、地址和更新时间。

        返回：仍匹配的数据库行；重注册或较新探测改变快照时返回 `None`。

        异常：数据库错误原样传播。
        """
        return await session.scalar(
            select(WorkerInstanceModel)
            .where(
                WorkerInstanceModel.instance_id == target.instance_id,
                WorkerInstanceModel.instance_key == target.instance_key,
                WorkerInstanceModel.service_principal == target.service_principal,
                WorkerInstanceModel.base_url == target.base_url,
                WorkerInstanceModel.updated_at == target.registration_updated_at,
                WorkerInstanceModel.status != WorkerInstanceStatus.DISABLED.value,
            )
            .with_for_update()
        )

    async def _now(self, session: AsyncSession) -> datetime:
        """读取 PostgreSQL 时钟或注入的测试时钟。

        参数：
        - `session`（AsyncSession）：当前数据库会话。

        返回：携带时区的健康更新时间。

        异常：注入时钟未带时区时抛出 ValueError。
        """
        if self._clock is not None:
            now = self._clock()
        else:
            now = await session.scalar(select(func.clock_timestamp()))
        if now is None or now.tzinfo is None:
            raise ValueError("worker_health_clock_must_be_timezone_aware")
        return now

    @staticmethod
    def _transition(row: WorkerInstanceModel) -> WorkerHealthTransition:
        """构造最新健康状态值对象。

        参数：
        - `row`（WorkerInstanceModel）：事务中已更新的数据库行。

        返回：不暴露 ORM 对象的状态转换快照。

        异常：此函数不主动抛出业务异常。
        """
        return WorkerHealthTransition(
            instance_id=row.instance_id,
            status=row.status,
            accepting_tasks=row.accepting_tasks,
            failure_count=row.consecutive_health_failures,
            success_count=row.consecutive_health_successes,
            updated_at=row.updated_at,
        )


__all__ = ["PostgresWorkerRegistryRepository"]
