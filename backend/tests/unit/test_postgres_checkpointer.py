"""PostgreSQL LangGraph checkpointer 生命周期测试。"""

from __future__ import annotations

import pytest

from infrastructure.persistence.postgres import checkpointer as module
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer


class _Saver:
    def __init__(self, *, fail_setup: bool = False) -> None:
        """记录 setup 调用，并按测试设置模拟初始化失败。"""
        self.fail_setup = fail_setup
        self.setup_called = False

    async def setup(self) -> None:
        """标记初始化已执行；fail_setup 为真时抛出测试异常。"""
        self.setup_called = True
        if self.fail_setup:
            raise RuntimeError("setup failed")


class _Context:
    def __init__(self, saver: _Saver, *, fail_enter: bool = False) -> None:
        """保存替身并配置是否模拟连接进入失败。"""
        self.saver = saver
        self.fail_enter = fail_enter
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _Saver:
        """模拟打开 checkpointer；配置失败时抛出连接异常。"""
        self.entered = True
        if self.fail_enter:
            raise RuntimeError("connection failed")
        return self.saver

    async def __aexit__(self, *_args: object) -> None:
        """记录资源释放，供生命周期断言使用。"""
        self.exited = True


@pytest.mark.asyncio
async def test_checkpointer_requires_start_and_releases_context(monkeypatch: pytest.MonkeyPatch) -> None:
    saver = _Saver()
    context = _Context(saver)
    monkeypatch.setattr(module.AsyncPostgresSaver, "from_conn_string", lambda _: context)
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="has not been started"):
        _ = resource.saver
    await resource.start()
    assert resource.saver is saver
    assert saver.setup_called and context.entered
    await resource.stop()
    assert context.exited
    with pytest.raises(RuntimeError, match="has not been started"):
        resource.get_saver()


@pytest.mark.asyncio
async def test_checkpointer_closes_context_when_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    saver = _Saver(fail_setup=True)
    context = _Context(saver)
    monkeypatch.setattr(module.AsyncPostgresSaver, "from_conn_string", lambda _: context)
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="setup failed"):
        await resource.start()
    assert context.entered and context.exited
    with pytest.raises(RuntimeError, match="has not been started"):
        _ = resource.saver


@pytest.mark.asyncio
async def test_checkpointer_does_not_exit_context_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接阶段失败时保留原始异常且不释放未进入的上下文。"""
    saver = _Saver()
    context = _Context(saver, fail_enter=True)
    monkeypatch.setattr(module.AsyncPostgresSaver, "from_conn_string", lambda _: context)
    resource = PostgresCheckpointer("postgresql://athena")

    with pytest.raises(RuntimeError, match="connection failed"):
        await resource.start()

    assert context.entered
    assert not context.exited
    with pytest.raises(RuntimeError, match="has not been started"):
        _ = resource.saver
