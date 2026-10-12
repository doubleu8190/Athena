"""PostgreSQL LangGraph checkpointer 生命周期测试。"""

from __future__ import annotations

import pytest

from infrastructure.persistence.postgres import checkpointer as module
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer


class _Saver:
    def __init__(self, *, fail_setup: bool = False) -> None:
        self.fail_setup = fail_setup
        self.setup_called = False

    async def setup(self) -> None:
        self.setup_called = True
        if self.fail_setup:
            raise RuntimeError("setup failed")


class _Context:
    def __init__(self, saver: _Saver) -> None:
        self.saver = saver
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _Saver:
        self.entered = True
        return self.saver

    async def __aexit__(self, *_args: object) -> None:
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
