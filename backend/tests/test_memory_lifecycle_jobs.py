"""记忆后台任务连续失败预算的单元回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from adapter.memory.model import (
    MemoryJobModel,
    MemoryScopeStateModel,
    MemorySettingModel,
)
from adapter.memory.repository.lifecycle import MemoryLifecycleRepositoryMixin
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    CompleteJobCommand,
    ExtractBatchMember,
    ExtractBatchPayload,
    ExtractPendingPayload,
    JobKind,
    JobStatus,
    ScanJobResult,
    ScanResult,
    ScanRunsPayload,
)

NOW = datetime(2026, 11, 14, tzinfo=UTC)


class _Transaction:
    """为无需数据库的任务状态单元测试提供空事务上下文。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class _Session:
    """只返回预置任务行的最小异步会话。"""

    def __init__(self, row: MemoryJobModel) -> None:
        self._row = row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def begin(self) -> _Transaction:
        return _Transaction()

    async def scalar(self, statement: object) -> MemoryJobModel:
        del statement
        return self._row


class _Repository(MemoryLifecycleRepositoryMixin):
    """把任务状态机连接到内存任务行。"""

    def __init__(self, row: MemoryJobModel) -> None:
        self._factory = lambda: _Session(row)


class _ApplySession:
    """按锁获取顺序返回 settings 和 extract job。"""

    def __init__(self, setting: MemorySettingModel, job: MemoryJobModel) -> None:
        self._rows = [setting, job]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> None:
        del statement

    async def scalar(self, statement: object) -> object:
        del statement
        return self._rows.pop(0)


class _ApplyRepository(MemoryLifecycleRepositoryMixin):
    """隔离正常批次提交所需的数据库上下文。"""

    def __init__(self, setting: MemorySettingModel, job: MemoryJobModel) -> None:
        self._factory = lambda: _ApplySession(setting, job)

    async def _lock_batch_context(
        self,
        session: object,
        batch: ExtractBatchPayload,
    ) -> tuple[
        dict[str, dict[str, datetime]],
        dict[str, MemoryScopeStateModel],
    ]:
        del session, batch
        return {}, {}

    @staticmethod
    def _validate_apply_plan(
        command: ApplyBatchCommand,
        scopes: dict[str, MemoryScopeStateModel],
    ) -> None:
        del command, scopes

    async def _owner_has_pending_runs(self, session: object, owner_id: str) -> bool:
        del session, owner_id
        return True


def _scan_job() -> MemoryJobModel:
    payload = ScanRunsPayload(enabled_boundary_at=NOW)
    return MemoryJobModel(
        id="memory-scan-job",
        dedupe_key="scan_runs:global",
        kind=JobKind.SCAN_RUNS.value,
        status=JobStatus.RUNNING.value,
        payload=payload.model_dump(mode="json"),
        result=None,
        namespace_id="__global__",
        scope_key=None,
        attempt_count=5,
        schema_retry_count=1,
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_successful_scan_resets_consecutive_failure_budget() -> None:
    """扫描成功重排后，下一轮一次临时失败仍应进入重试。"""

    async def scenario() -> None:
        row = _scan_job()
        repository = _Repository(row)
        success = ScanJobResult(
            result=ScanResult(
                scanned=1,
                awakened=1,
                round_complete=True,
            )
        )

        assert await repository.complete_job(
            CompleteJobCommand(job_id=row.id, result=success)
        )
        assert row.status == JobStatus.QUEUED.value
        assert row.attempt_count == 0
        assert row.schema_retry_count == 0

        # 模拟下一次 claim 后首次出现临时基础设施错误。
        row.status = JobStatus.RUNNING.value
        row.attempt_count = 1
        assert await repository.complete_job(
            CompleteJobCommand(
                job_id=row.id,
                result=None,
                safe_error_code="memory_scan_temporarily_failed",
                retryable=True,
            )
        )
        assert row.status == JobStatus.QUEUED.value

    asyncio.run(scenario())


def test_normal_batch_completion_advances_generation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使批次无候选，正常完成也必须原子推进一次 generation。"""

    async def no_op_mark_batch_runs(
        session: object,
        batch: ExtractBatchPayload,
        *,
        outcome: object,
        safe_error_code: str | None,
        now: datetime,
    ) -> None:
        del session, batch, outcome, safe_error_code, now

    monkeypatch.setattr(
        "adapter.memory.repository.batch_extract._mark_batch_runs_locked",
        no_op_mark_batch_runs,
    )
    setting = MemorySettingModel(
        namespace_id="owner-1",
        read_enabled=True,
        auto_extract_enabled=True,
        generation=3,
    )
    job = MemoryJobModel(
        id="memory-extract-job",
        dedupe_key="extract_pending:owner-1",
        kind=JobKind.EXTRACT_PENDING.value,
        status=JobStatus.RUNNING.value,
        payload=ExtractPendingPayload(owner_id="owner-1").model_dump(mode="json"),
        result=None,
        namespace_id="owner-1",
        scope_key=None,
        attempt_count=1,
        schema_retry_count=0,
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    batch = ExtractBatchPayload(
        batch_id="batch-1",
        owner_id="owner-1",
        generation=3,
        members=(
            ExtractBatchMember(
                run_id="run-1",
                session_id="session-1",
                source_message_ids=("message-1",),
                source_cutoff_at=NOW,
            ),
        ),
        model_profile_version_id="model-1",
        scope_keys=("personal",),
        created_at=NOW,
        extraction_complete=True,
        decisions_complete=True,
    )
    repository = _ApplyRepository(setting, job)

    result = asyncio.run(
        repository.apply_batch(
            ApplyBatchCommand(
                job_id=job.id,
                batch=batch,
                changes=(),
            )
        )
    )

    assert result.status == "applied"
    assert setting.generation == 4
    assert job.status == JobStatus.QUEUED.value
