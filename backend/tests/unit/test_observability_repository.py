"""Progress 仓储终态与幂等重放边界的单元测试。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from application.observability import ProgressExecutionTerminalError
from application.observability.contracts import WorkerProgressContext
from infrastructure.persistence.postgres.models import RunProgressModel
from infrastructure.persistence.postgres.repositories.observability_repository import (
    PostgresObservabilityRepository,
)


class _FakeSession:
    """为不依赖 PostgreSQL 的 Progress 分支提供最小会话替身。"""

    def __init__(
        self,
        *,
        execution_status: str,
        existing_rows: list[RunProgressModel | None],
    ) -> None:
        self._execution_status = execution_status
        self._existing_rows = list(existing_rows)
        self._execution_id = f"execution-{uuid4().hex}"
        self._worker_id = f"worker-{uuid4().hex}"
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> AbstractAsyncContextManager[None]:
        return _AsyncContextManager()

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("progress_lock")
        return None

    async def get(self, *_args: object, **_kwargs: object) -> Any:
        self.calls.append("execution_lock")
        return SimpleNamespace(
            execution_id=self._execution_id,
            instance_id=self._worker_id,
            status=self._execution_status,
        )

    async def scalar(self, *_args: object, **_kwargs: object) -> Any:
        return self._existing_rows.pop(0)


class _AsyncContextManager(AbstractAsyncContextManager[None]):
    """提供空事务上下文。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Rows:
    def __init__(self, values: list[tuple[object, ...]]) -> None:
        self._values = values

    def all(self) -> list[tuple[object, ...]]:
        return self._values


class _ProgressListSession:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _ProgressListSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
        return _Rows(self._rows)


class _OneOrNoneResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def one_or_none(self) -> object:
        return self._value


class _ContextSession(_ProgressListSession):
    def __init__(self, value: object) -> None:
        super().__init__([])
        self._value = value

    async def execute(self, *_args: object, **_kwargs: object) -> _OneOrNoneResult:
        return _OneOrNoneResult(self._value)


def _context(session: _FakeSession) -> WorkerProgressContext:
    return WorkerProgressContext(
        execution_id=session._execution_id,
        task_id="task-1",
        run_id="run-1",
        session_id="session-1",
        worker_id=session._worker_id,
        worker_name="Worker A",
        task_title="整理资料",
    )


def _row(*, content: str, sequence: int) -> RunProgressModel:
    return RunProgressModel(
        progress_id=f"progress-{sequence}",
        session_id="session-1",
        run_id="run-1",
        execution_id="execution-1",
        task_id="task-1",
        worker_id="worker-1",
        source="worker",
        scope_key="execution:execution-1",
        sequence=sequence,
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ingest_sequence=sequence,
    )


@pytest.mark.asyncio
async def test_worker_progress_exact_replay_survives_terminal_execution() -> None:
    """精确重放先于终态检查，网络重试仍可取回原 Progress。"""
    session = _FakeSession(
        execution_status="completed",
        existing_rows=[_row(content="正在读取资料", sequence=1)],
    )
    repository = PostgresObservabilityRepository(lambda: session)

    replay = await repository.append_worker_progress(
        _context(session), sequence=1, content="正在读取资料"
    )

    assert replay.progress_id == "progress-1"
    assert replay.sequence == 1
    assert replay.content == "正在读取资料"
    assert replay.worker_name == "Worker A"
    assert replay.task_title == "整理资料"
    assert session.calls[:2] == ["progress_lock", "execution_lock"]


@pytest.mark.asyncio
async def test_progress_replay_includes_worker_and_task_display_metadata() -> None:
    """SSE 历史补发读取的 Progress 应带可读 Worker 名称与任务标题。"""
    session = _ProgressListSession(
        [
            (
                _row(content="正在读取资料", sequence=1),
                "Worker A",
                "整理资料",
            )
        ]
    )
    repository = PostgresObservabilityRepository(lambda: session)

    progress = await repository.list_progress_after("session-1", 0)

    assert len(progress) == 1
    assert progress[0].worker_name == "Worker A"
    assert progress[0].task_title == "整理资料"


@pytest.mark.asyncio
async def test_worker_progress_context_falls_back_to_instance_id() -> None:
    """执行未保存展示名称时，上下文使用认证实例 ID 作为展示名。"""
    session = _ContextSession(
        SimpleNamespace(
            execution_id="execution-1",
            task_id="task-1",
            instance_id="worker-instance-1",
            worker_name=None,
            run_id="run-1",
            session_id="session-1",
            objective="整理资料",
        )
    )
    repository = PostgresObservabilityRepository(lambda: session)

    context = await repository.worker_progress_context(
        execution_id="execution-1",
        worker_id="worker-instance-1",
    )

    assert context is not None
    assert context.worker_name == "worker-instance-1"
    assert context.task_title == "整理资料"


@pytest.mark.asyncio
async def test_worker_progress_new_sequence_after_terminal_raises_domain_error() -> None:
    """执行终态后的新序号必须抛出可被 HTTP 层映射的领域异常。"""
    session = _FakeSession(
        execution_status="completed",
        existing_rows=[None],
    )
    repository = PostgresObservabilityRepository(lambda: session)

    with pytest.raises(
        ProgressExecutionTerminalError,
        match="worker_execution_already_terminal",
    ):
        await repository.append_worker_progress(
            _context(session), sequence=2, content="迟到进度"
        )
