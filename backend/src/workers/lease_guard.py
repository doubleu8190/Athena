"""持有执行期间续约并中断已失去租约的本地调用。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from inspect import iscoroutine
from typing import TypeVar

from domain.leases import LeaseCenterPort, LeaseGrant, LeaseLostError
from shared.logging import get_logger

_Result = TypeVar("_Result")
logger = get_logger(__name__)


class LeaseGuard:
    """在一项或多项租约的有效期内运行一次异步调用。"""

    def __init__(
        self,
        center: LeaseCenterPort,
        grants: Sequence[LeaseGrant],
        *,
        lease_seconds: float,
    ) -> None:
        """保存租约续期端口、当前授权和租期。"""
        if not grants or lease_seconds <= 0:
            raise ValueError("lease grants and positive duration are required")
        self._center = center
        self._grants = list(grants)
        self._lease_seconds = lease_seconds
        self._lost_key: tuple[str, str] | None = None

    @property
    def grants(self) -> tuple[LeaseGrant, ...]:
        """返回最近续期后的授权快照。"""
        return tuple(self._grants)

    async def run(self, invocation: Awaitable[_Result]) -> _Result:
        """心跳续期期间等待调用，失去任一租约时取消调用。"""
        try:
            for grant in self._grants:
                await self._center.assert_current(grant)
        except Exception:
            if iscoroutine(invocation):
                invocation.close()
            raise
        invocation_task = asyncio.ensure_future(invocation)
        heartbeat_task = asyncio.create_task(
            self._maintain(invocation_task), name="athena-lease-heartbeat"
        )
        try:
            result = await invocation_task
        except asyncio.CancelledError:
            if self._lost_key is not None:
                raise LeaseLostError(
                    next(
                        grant.key
                        for grant in self._grants
                        if (grant.key.resource_type, grant.key.resource_id)
                        == self._lost_key
                    )
                )
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if self._lost_key is not None:
            resource_type, resource_id = self._lost_key
            raise LeaseLostError(
                next(
                    grant.key
                    for grant in self._grants
                    if (grant.key.resource_type, grant.key.resource_id)
                    == (resource_type, resource_id)
                )
            )
        return result

    async def _maintain(self, invocation: asyncio.Future[_Result]) -> None:
        """周期续约所有授权；任何一个授权失效都中断当前调用。"""
        interval = max(0.1, self._lease_seconds / 3)
        renewal_timeout = max(0.05, min(5.0, interval / 2))
        while not invocation.done():
            await asyncio.sleep(interval)
            if invocation.done():
                return
            for index, grant in enumerate(self._grants):
                try:
                    renewed = await asyncio.wait_for(
                        self._center.renew(
                            grant, lease_seconds=self._lease_seconds
                        ),
                        timeout=renewal_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "execution_lease_renewal_failed",
                        resource_type=grant.key.resource_type,
                        resource_id=grant.key.resource_id,
                        fencing_token=grant.fencing_token,
                        exc_info=True,
                    )
                    renewed = None
                if renewed is None:
                    self._lost_key = (
                        grant.key.resource_type,
                        grant.key.resource_id,
                    )
                    invocation.cancel()
                    return
                self._grants[index] = renewed


__all__ = ["LeaseGuard"]
