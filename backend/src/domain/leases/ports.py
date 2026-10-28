"""租约中心跨领域可用的执行期端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import LeaseGrant, LeaseKey, LeaseRecord


class LeaseCenter(Protocol):
    """不暴露数据库会话的租约中心领域接口。"""

    async def acquire(
        self, key: LeaseKey, *, owner_id: str, lease_seconds: float
    ) -> LeaseGrant | None:
        """申请资源执行权；资源被有效租约占用时返回 None。"""

    async def renew(self, grant: LeaseGrant, *, lease_seconds: float) -> LeaseGrant | None:
        """续期仍由当前 grant 持有的租约；身份失效时返回 None。"""

    async def assert_current(self, grant: LeaseGrant) -> None:
        """校验 grant 仍为数据库中有效的当前授权。"""

    async def finish(self, grant: LeaseGrant, *, completed: bool = False) -> bool:
        """结束授权；资源已处理完时标为完成，否则释放以便重试。"""

    async def revoke(self, key: LeaseKey, *, expected_token: int) -> bool:
        """撤销指定的当前代次。"""

    async def acknowledge_stopped(
        self, grant: LeaseGrant, *, completed: bool = False
    ) -> bool:
        """确认当前代次已停止，必要时同时关闭该资源。"""

    async def claimable_keys(
        self,
        resource_type: str,
        *,
        limit: int,
        after_resource_id: str | None = None,
    ) -> list[LeaseKey]:
        """返回可尝试领取的资源身份；调用方仍需验证业务状态。"""

    async def get(self, key: LeaseKey) -> LeaseRecord | None:
        """读取资源租约状态，供取消和管理查询使用。"""


LeaseCenterPort = LeaseCenter

__all__ = ["LeaseCenter", "LeaseCenterPort"]
