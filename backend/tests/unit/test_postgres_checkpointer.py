"""PostgreSQL LangGraph checkpointer 生命周期测试。"""

from __future__ import annotations

import pytest

from infrastructure.persistence.postgres import checkpointer as module
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer


class _Saver:
    def __init__(self, pool: object, *, fail_setup: bool = False) -> None:
        """保存连接池替身，并配置初始化结果。"""
        self.pool = pool
        self.fail_setup = fail_setup
        self.setup_called = False

    async def setup(self) -> None:
        """模拟创建 checkpoint schema。"""
        self.setup_called = True
        if self.fail_setup:
            raise RuntimeError("setup failed")


class _Pool:
    instances: list[_Pool] = []
    fail_open = False
    fail_wait = False

    @staticmethod
    async def check_connection(_connection: object) -> None:
        """模拟池连接健康检查回调。"""

    def __init__(self, **kwargs: object) -> None:
        """记录连接池参数和生命周期调用。"""
        self.kwargs = kwargs
        self.opened = False
        self.waited = False
        self.closed = False
        type(self).instances.append(self)

    async def open(self) -> None:
        """模拟连接池启动。"""
        if type(self).fail_open:
            raise RuntimeError("connection failed")
        self.opened = True

    async def wait(self) -> None:
        """模拟等待最小连接数就绪。"""
        if type(self).fail_wait:
            raise RuntimeError("pool wait failed")
        self.waited = True

    async def close(self) -> None:
        """模拟关闭连接池。"""
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool() -> None:
    """每个测试使用独立的连接池替身状态。"""
    _Pool.instances = []
    _Pool.fail_open = False
    _Pool.fail_wait = False


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, saver: _Saver) -> None:
    """将连接池和 saver 替换为生命周期可观察的测试替身。"""
    monkeypatch.setattr(module, "AsyncConnectionPool", _Pool)
    monkeypatch.setattr(module, "AsyncPostgresSaver", lambda pool: _bind_saver(saver, pool))


def _bind_saver(saver: _Saver, pool: object) -> _Saver:
    """将本次测试连接池绑定到 saver 替身。"""
    saver.pool = pool
    return saver


@pytest.mark.asyncio
async def test_checkpointer_uses_health_checked_pool_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = _Saver(None)
    _patch_dependencies(monkeypatch, saver)
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="has not been started"):
        _ = resource.saver
    await resource.start()
    await resource.start()

    pool = _Pool.instances[0]
    assert len(_Pool.instances) == 1
    assert pool.kwargs["check"] is _Pool.check_connection
    assert pool.kwargs["open"] is False
    assert pool.opened and pool.waited
    assert saver.pool is pool and saver.setup_called
    assert resource.saver is saver

    await resource.stop()
    assert pool.closed
    with pytest.raises(RuntimeError, match="has not been started"):
        resource.get_saver()


@pytest.mark.asyncio
async def test_checkpointer_closes_pool_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = _Saver(None, fail_setup=True)
    _patch_dependencies(monkeypatch, saver)
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="setup failed"):
        await resource.start()

    assert _Pool.instances[0].closed
    with pytest.raises(RuntimeError, match="has not been started"):
        _ = resource.saver


@pytest.mark.asyncio
async def test_checkpointer_closes_pool_when_wait_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(monkeypatch, _Saver(None))
    _Pool.fail_wait = True
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="pool wait failed"):
        await resource.start()

    assert _Pool.instances[0].opened
    assert _Pool.instances[0].closed


@pytest.mark.asyncio
async def test_checkpointer_closes_pool_when_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(monkeypatch, _Saver(None))
    _Pool.fail_open = True
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="connection failed"):
        await resource.start()

    assert _Pool.instances[0].closed
