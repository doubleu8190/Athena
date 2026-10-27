"""租约中心跨领域可用的执行期端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import LeaseGrant, LeaseKey


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

    async def finish(self, grant: LeaseGrant) -> bool:
        """正常结束本次授权并释放执行权。"""

    async def revoke(self, key: LeaseKey, *, expected_token: int) -> bool:
        """撤销指定的当前代次。"""

    async def acknowledge_stopped(self, grant: LeaseGrant) -> bool:
        """确认当前代次已停止并释放执行权。"""


LeaseCenterPort = LeaseCenter

__all__ = ["LeaseCenter", "LeaseCenterPort"]
