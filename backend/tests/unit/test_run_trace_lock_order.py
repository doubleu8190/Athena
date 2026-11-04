"""Run claim/finalization 与并发 Trace 写入的锁序单元回归。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

import pytest

from domain.leases import LeaseKey
from infrastructure.persistence.postgres.repositories import (
    run_repository as repository_module,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from tests.fakes.leases import lease_grant


class _Transaction(AbstractAsyncContextManager[None]):
    """提供空事务入口。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Rows:
    """模拟 SQLAlchemy scalar result 的 `.all()`。"""

    def all(self) -> list[object]:
        return []


class _Session:
    """记录 Trace advisory 与第一条领域行锁的调用顺序。"""

    def __init__(self, *, scalar_results: list[Any]) -> None:
        self.calls: list[str] = []
        self._scalar_results = list(scalar_results)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def scalar(self, statement: object, *_args: object, **_kwargs: object) -> Any:
        if getattr(statement, "_for_update_arg", None) is not None:
            self.calls.append("row_lock")
        return self._scalar_results.pop(0)

    async def get(self, *_args: object, **kwargs: object) -> Any:
        if kwargs.get("with_for_update"):
            self.calls.append("row_lock")
        return None

    async def scalars(
        self, _statement: object, *_args: object, **_kwargs: object
    ) -> _Rows:
        return _Rows()


class _ClaimLeaseCenter:
    """只返回一个候选 Run，并在下一次分页直接结束。"""

    def __init__(self) -> None:
        self.calls = 0

    async def claimable_keys_in_transaction(
        self,
        _session: _Session,
        _resource_type: str,
        *,
        limit: int,
        after_resource_id: str | None,
    ) -> list[LeaseKey]:
        assert limit == 512
        self.calls += 1
        if after_resource_id is not None:
            return []
        return [LeaseKey("root_execution", "run-1")]


def _patch_trace_lock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: _Session,
) -> None:
    """替换 advisory helper，并验证每个入口使用当前 Run 身份。"""

    async def fake_trace_lock(current: _Session, run_id: str) -> None:
        assert current is session
        assert run_id == "run-1"
        current.calls.append("trace_lock")

    monkeypatch.setattr(
        repository_module,
        "lock_trace_span_scope",
        fake_trace_lock,
    )


@pytest.mark.asyncio
async def test_claim_next_run_locks_trace_scope_before_session_and_run_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim 必须无锁预读候选，取得 advisory 后才锁 Session/Run。"""
    session = _Session(scalar_results=["run-1", None])
    _patch_trace_lock(monkeypatch, session=session)
    repository = PostgresRunRepository(
        lambda: session,
        lease_center=_ClaimLeaseCenter(),
    )

    result = await repository.claim_next_run()

    assert result is None
    assert session.calls[:2] == ["trace_lock", "row_lock"]


@pytest.mark.asyncio
async def test_finalize_stop_locks_trace_scope_before_session_and_run_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未领取 Run 的终态收尾必须先取得 Trace advisory。"""
    session = _Session(
        scalar_results=[
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "session-1",
            None,
        ]
    )
    _patch_trace_lock(monkeypatch, session=session)
    repository = PostgresRunRepository(lambda: session)

    result = await repository.finalize_stop("run-1")

    assert result is False
    assert session.calls[:2] == ["trace_lock", "row_lock"]


@pytest.mark.asyncio
async def test_run_completion_locks_trace_scope_before_session_and_run_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常完成事务必须先取得 Trace advisory，再锁 Session/Run。"""
    session = _Session(scalar_results=["session-1", None])
    _patch_trace_lock(monkeypatch, session=session)
    repository = PostgresRunRepository(lambda: session)

    result = await repository.complete(
        "run-1",
        {},
        lease=lease_grant("root_execution", "run-1"),
    )

    assert result is False
    assert session.calls[:2] == ["trace_lock", "row_lock"]
