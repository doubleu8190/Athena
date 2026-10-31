"""定时探测注册 Worker 并更新可分配状态。"""

from __future__ import annotations

import asyncio

from application.orchestration.worker_registry import WorkerRegistryService
from infrastructure.integrations.worker_http import (
    PinnedWorkerHttpClient,
    WorkerOriginRejected,
)
from domain.orchestration.worker_registry import WorkerHealthProbeTarget
from shared.logging import get_logger

logger = get_logger(__name__)


class WorkerHealthMonitor:
    """按心跳阈值探测实例，忽略已经过期的并发探测快照。"""

    def __init__(
        self,
        registry: WorkerRegistryService,
        client: PinnedWorkerHttpClient,
        *,
        interval_seconds: float = 15.0,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        max_concurrency: int = 16,
    ) -> None:
        """校验并保存健康扫描设置。

        参数：
        - `registry`（WorkerRegistryService）：读取注册快照并写入 CAS 健康状态的用例。
        - `client`（PinnedWorkerHttpClient）：使用 DNS pinning 探测的 Worker HTTP 客户端。
        - `interval_seconds`（float）：每轮探测间隔，约束为 1–300 秒。
        - `failure_threshold`（int）：连续失败达到该次数后摘除实例，约束为正整数。
        - `recovery_threshold`（int）：连续 ready 响应达到该次数后恢复，约束为正整数。
        - `max_concurrency`（int）：单进程并发探测数，约束为正整数。

        返回：无。

        异常：ValueError 表示心跳间隔、计数阈值或并发上限无效。
        """
        if not 1 <= interval_seconds <= 300:
            raise ValueError("worker_health_interval_out_of_range")
        if failure_threshold < 1 or recovery_threshold < 1 or max_concurrency < 1:
            raise ValueError("worker_health_threshold_or_concurrency_invalid")
        self._registry = registry
        self._client = client
        self._interval_seconds = interval_seconds
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动单个健康探测循环，重复调用保持幂等。"""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-worker-health-monitor")

    async def stop(self) -> None:
        """取消当前扫描批次并等待循环退出。"""
        self._stop.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        """扫描所有非禁用 Worker；一次实例异常不会中断后续实例探测。"""
        while not self._stop.is_set():
            try:
                targets = await self._registry.list_health_probe_targets()
                await asyncio.gather(*(self._probe(target) for target in targets))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("worker_health_scan_failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _probe(self, target: WorkerHealthProbeTarget) -> None:
        """探测单个注册快照，并只向注册表写入可信结果。

        参数：
        - `target`（WorkerHealthProbeTarget）：待探测的固定实例快照。

        返回：无。

        异常：探测和持久化错误会捕获并记录实例 ID，避免遗漏其他实例。
        """
        async with self._semaphore:
            try:
                result = await self._client.health_check(target)
                if result is None:
                    await self._registry.record_health_failure(
                        target, failure_threshold=self._failure_threshold
                    )
                elif result.status == "identity_mismatch":
                    await self._registry.record_identity_mismatch(target)
                    logger.error(
                        "worker_health_instance_key_mismatch",
                        extra={"worker_instance_id": target.instance_id},
                    )
                else:
                    await self._registry.record_health_success(
                        target,
                        result,
                        recovery_threshold=self._recovery_threshold,
                    )
            except asyncio.CancelledError:
                raise
            except WorkerOriginRejected as exc:
                await self._registry.record_health_failure(
                    target, failure_threshold=self._failure_threshold
                )
                logger.error(
                    "worker_health_address_rejected",
                    extra={"worker_instance_id": target.instance_id, "reason": str(exc)},
                )
            except Exception:
                logger.error(
                    "worker_health_probe_failed",
                    extra={"worker_instance_id": target.instance_id},
                    exc_info=True,
                )


__all__ = ["WorkerHealthMonitor"]
