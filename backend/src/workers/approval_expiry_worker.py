"""处理已经到期且仍待处理的人工审批。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ApprovalExpiryWorker:
    """定期把到期审批变成显式拒绝并恢复对应 Worker。"""

    def __init__(
        self,
        approvals: Any,
        *,
        poll_interval: float = 1.0,
        batch_limit: int = 64,
    ) -> None:
        """保存审批服务和轮询边界。

        Args:
            approvals: 提供 `expire_due_approvals` 的审批应用服务。
            poll_interval: 无到期记录时再次检查前等待的秒数。
            batch_limit: 单次事务最多处理的到期审批数。

        Raises:
            ValueError: 轮询间隔或批量上限不是正数。
        """
        if poll_interval <= 0 or batch_limit < 1:
            raise ValueError("approval expiry polling and batch limit must be positive")
        self._approvals = approvals
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动唯一的到期审批扫描循环。"""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self.run(), name="athena-approval-expiry-worker"
        )

    async def stop(self) -> None:
        """停止扫描并等待当前短事务完成。"""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        """持续扫描到期审批，失败时保留记录并在下一轮重试。"""
        while not self._stop.is_set():
            try:
                expired = await self._approvals.expire_due_approvals(
                    limit=self._batch_limit
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Approval expiry scan failed")
                expired = 0
            if expired >= self._batch_limit:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass


__all__ = ["ApprovalExpiryWorker"]
