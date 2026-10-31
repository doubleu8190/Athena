"""Worker 健康状态与可分配能力查询用例。"""

from __future__ import annotations

from typing import Protocol

from domain.orchestration.worker_registry import (
    WorkerAvailableInstance,
    WorkerHealthProbeResult,
    WorkerHealthProbeTarget,
    WorkerHealthTransition,
)


class WorkerRegistryRepository(Protocol):
    """健康探测及 Worker 可用能力读取端口。"""

    async def list_health_probe_targets(self) -> list[WorkerHealthProbeTarget]: ...

    async def record_health_success(
        self,
        target: WorkerHealthProbeTarget,
        result: WorkerHealthProbeResult,
        *,
        recovery_threshold: int,
    ) -> WorkerHealthTransition | None: ...

    async def record_health_failure(
        self,
        target: WorkerHealthProbeTarget,
        *,
        failure_threshold: int,
    ) -> WorkerHealthTransition | None: ...

    async def record_identity_mismatch(
        self, target: WorkerHealthProbeTarget
    ) -> WorkerHealthTransition | None: ...

    async def list_available_workers(self) -> list[WorkerAvailableInstance]: ...

    async def resolve_execution_instance(
        self, service_principal: str, execution_id: str
    ) -> str | None: ...

    async def resolve_cancel_instance(
        self,
        service_principal: str,
        execution_id: str,
        cancel_command_id: str,
    ) -> str | None: ...


class WorkerRegistrationAddressValidator(Protocol):
    """注册地址策略的应用边界；DNS 和 HTTP 实现留在基础设施层。"""

    async def validate_registration_origin(self, base_url: str) -> None: ...


class WorkerRegistryService:
    """提供 Worker 健康目标、探测结果和可用能力查询。"""

    def __init__(self, repository: WorkerRegistryRepository) -> None:
        """初始化注册表查询与健康更新用例。

        参数：
        - `repository`（WorkerRegistryRepository）：负责注册快照 CAS 和可用能力读取的仓储端口。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def list_health_probe_targets(self) -> list[WorkerHealthProbeTarget]:
        """读取当前需要探测的实例注册快照。

        参数：无。

        返回：所有未禁用实例的不可变探测快照。

        异常：持久化错误原样传播。
        """
        return await self._repository.list_health_probe_targets()

    async def record_health_success(
        self,
        target: WorkerHealthProbeTarget,
        result: WorkerHealthProbeResult,
        *,
        recovery_threshold: int,
    ) -> WorkerHealthTransition | None:
        """应用有效健康响应；过期快照不会覆盖较新的注册。

        参数：
        - `target`（WorkerHealthProbeTarget）：发起请求时读取的实例快照。
        - `result`（WorkerHealthProbeResult）：通过 v1 schema 且实例键匹配的健康响应。
        - `recovery_threshold`（int）：连续 ready 探测后恢复接单的次数。

        返回：事务 CAS 更新成功时的状态，否则为 `None`。

        异常：ValueError 表示恢复阈值小于 1。
        """
        if recovery_threshold < 1:
            raise ValueError("worker_health_recovery_threshold_invalid")
        return await self._repository.record_health_success(
            target, result, recovery_threshold=recovery_threshold
        )

    async def record_health_failure(
        self, target: WorkerHealthProbeTarget, *, failure_threshold: int
    ) -> WorkerHealthTransition | None:
        """应用一次网络或协议失败并在阈值处摘除实例。

        参数：
        - `target`（WorkerHealthProbeTarget）：发起请求时读取的实例快照。
        - `failure_threshold`（int）：连续失败后移出可分配列表的次数。

        返回：事务 CAS 更新成功时的状态，否则为 `None`。

        异常：ValueError 表示失败阈值小于 1。
        """
        if failure_threshold < 1:
            raise ValueError("worker_health_failure_threshold_invalid")
        return await self._repository.record_health_failure(
            target, failure_threshold=failure_threshold
        )

    async def record_identity_mismatch(
        self, target: WorkerHealthProbeTarget
    ) -> WorkerHealthTransition | None:
        """立即停止派发到回显了错误实例键的路由。

        参数：
        - `target`（WorkerHealthProbeTarget）：唯一可信的登记实例快照；不采纳响应内其他路由键。

        返回：事务 CAS 更新成功时的新 not_ready 状态，否则为 `None`。

        异常：持久化错误原样传播。
        """
        return await self._repository.record_identity_mismatch(target)

    async def list_available_workers(self) -> list[WorkerAvailableInstance]:
        """读取健康且主动接单的能力快照。

        参数：无。

        返回：仅包含 status=ready 且 accepting_tasks=true 的 Worker 实例与契约。

        异常：持久化错误原样传播。
        """
        return await self._repository.list_available_workers()

    async def resolve_execution_instance(
        self, service_principal: str, execution_id: str
    ) -> str | None:
        """按数据库分配关系解析结果提交方的实例身份。

        参数：
        - `service_principal`（str）：Bearer 认证确定的服务主体。
        - `execution_id`（str）：结果路径声明的 Execution ID。

        返回：仅当该主体拥有 Execution 实例时返回实例 ID，否则返回 `None`。

        异常：数据库错误原样传播。
        """
        return await self._repository.resolve_execution_instance(
            service_principal, execution_id
        )

    async def resolve_cancel_instance(
        self,
        service_principal: str,
        execution_id: str,
        cancel_command_id: str,
    ) -> str | None:
        """同时绑定取消命令、Execution 路径与服务主体。

        参数：
        - `service_principal`（str）：Bearer 认证确定的服务主体。
        - `execution_id`（str）：停止确认路径中的 Execution ID。
        - `cancel_command_id`（str）：请求内容绑定的原取消命令 ID。

        返回：命令和执行均归该主体实例所有时返回实例 ID，否则返回 `None`。

        异常：数据库错误原样传播。
        """
        return await self._repository.resolve_cancel_instance(
            service_principal, execution_id, cancel_command_id
        )


__all__ = [
    "WorkerRegistrationAddressValidator",
    "WorkerRegistryRepository",
    "WorkerRegistryService",
]
