"""Worker 健康监视器停止边界测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from domain.orchestration.worker_registry import WorkerHealthProbeTarget
from workers.worker_health_monitor import WorkerHealthMonitor


@pytest.mark.asyncio
async def test_stop_cancels_an_in_flight_probe_batch() -> None:
    """停止监视器时取消卡住的探测，不等待当前批次自然结束。"""
    target = WorkerHealthProbeTarget(
        instance_id="instance",
        instance_key="key",
        service_principal="worker",
        base_url="https://worker.example",
        registration_updated_at=datetime.now(timezone.utc),
    )
    probe_started = asyncio.Event()

    class Registry:
        async def list_health_probe_targets(self):
            return [target]

    class Client:
        async def health_check(self, _target):
            probe_started.set()
            await asyncio.Event().wait()

    monitor = WorkerHealthMonitor(Registry(), Client())
    await monitor.start()
    await asyncio.wait_for(probe_started.wait(), timeout=1)

    await asyncio.wait_for(monitor.stop(), timeout=1)
    assert monitor._task is None
