"""Claim and execute queued Runs."""

from __future__ import annotations

import asyncio
from domain.leases import LeaseCenterPort, LeaseLostError

from application.runs.execution_service import RunExecutionService
from backend.src.shared.logging import get_logger
from domain.runs import RunCommand, RunConsumerPort
from workers.lease_guard import LeaseGuard

logger = get_logger(__name__)


class RunConsumer:
    """轮询 Run 队列，并通过数据库会话锁避免多 Worker 并行执行。"""

    def __init__(
        self,
        queue: RunConsumerPort,
        execution: RunExecutionService,
        *,
        poll_interval: float = 0.25,
        lease_seconds: float = 60.0,
        lease_center: LeaseCenterPort | None = None,
    ) -> None:
        """创建 Run 消费器。

        参数：queue 提供排队、租约和终态操作；execution 执行 Root 调用；
        poll_interval 是空队列轮询间隔；lease_seconds 是 Root 租约时长。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._queue = queue
        self._leases = lease_center or getattr(queue, "_leases")
        self._execution = execution
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """启动后台轮询任务。"""
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-run-consumer")

    async def stop(self) -> None:
        """停止轮询并取消本进程中的未完成 Root 调用。"""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """持续领取并并发执行不同会话的 Run。"""
        while not self._stop.is_set():
            try:
                await self._queue.reconcile_expired_execution_leases()
                invocation = await self._queue.claim_next_run(
                    lease_seconds=self._lease_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Run queue polling failed", exc_info=True)
                invocation = None
            if invocation is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(
                self._execute_run(invocation),
                name=f"athena-run:{invocation.run_id}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            await asyncio.sleep(0)

    async def _execute_run(self, invocation: RunCommand) -> None:
        """执行一次 Root 调用并回收租约或完成停止收尾。"""
        try:
            if invocation.lease is None:
                raise RuntimeError("root_execution_lease_missing")
            await LeaseGuard(
                self._leases,
                [invocation.lease],
                lease_seconds=self._lease_seconds,
            ).run(self._execution.execute(invocation))
        except LeaseLostError:
            logger.warning("Root execution lease lost", run_id=invocation.run_id)
        except asyncio.CancelledError:
            if invocation.run_id:
                await self._execution.fail(
                    invocation, {"code": "root_execution_interrupted"}
                )
            logger.info("Run invocation stopped", run_id=invocation.run_id)
            return
        except Exception as exc:
            logger.error("Run invocation failed", run_id=invocation.run_id, exc_info=True)
            await self._execution.fail(
                invocation,
                {"code": "run_execution_failed", "message": str(exc) or type(exc).__name__},
            )
        finally:
            if invocation.lease is not None:
                await self._leases.acknowledge_stopped(invocation.lease)
            if invocation.run_id:
                await self._queue.finalize_stop(invocation.run_id)


__all__ = ["RunConsumer"]
