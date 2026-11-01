"""供已登录管理界面读取 Worker 注册快照的路由。"""

from __future__ import annotations


from fastapi import APIRouter

from application.orchestration.worker_registry import WorkerRegistryService
from interfaces.http.dto.worker_registry import WorkerRegistryInstanceResponse


def build_worker_registry_management_router(
    registry_service: WorkerRegistryService,
) -> APIRouter:
    """组装仅供浏览器会话访问的只读注册中心查询路由。

    参数：
    - `registry_service`（WorkerRegistryService]）：返回注册中心查询服务。

    返回：包含完整实例快照查询的 FastAPI 路由器。

    异常：持久化错误由应用异常处理器转换为 HTTP 错误。
    """
    router = APIRouter(tags=["worker-registry-management"])

    @router.get(
        "/worker-registry/instances",
        response_model=list[WorkerRegistryInstanceResponse],
    )
    async def list_registry_instances() -> list[WorkerRegistryInstanceResponse]:
        """返回注册中心的完整只读实例列表。

        参数：无。

        返回：含健康、接单、有效容量和不可变契约的实例数组。

        异常：数据库查询错误原样传播。
        """
        snapshots = await registry_service.list_registry_instances()
        return [
            WorkerRegistryInstanceResponse.model_validate(snapshot)
            for snapshot in snapshots
        ]

    return router


__all__ = ["build_worker_registry_management_router"]
