"""单 Memory Worker 的联合提取、原子丢弃与失败收敛测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from app.memory.extraction import (
    ExtractionInfrastructureError,
    ExtractionSchemaError,
    MemoryExtractionService,
)
from app.memory.worker import BACKGROUND_JOB_KINDS, MemoryWorker, MemoryWorkerConfig
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.profile_ports import ModelProfileRepository
from domain.memory.lifecycle import (
    ApplyBatchCommand,
    CleanupScopePayload,
    CompleteJobCommand,
    ExtractBatchPayload,
    ExtractBatchResult,
    ExtractPendingPayload,
    JobKind,
    JobStatus,
    MemoryJob,
    MemoryPendingRun,
    MemorySourceMessage,
    PreparedBatchChange,
    RunMemoryContextState,
    ScanResult,
)
from domain.memory.ports import MemoryLifecycleRepositoryPort

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def _runtime() -> RuntimeModelProfile:
    """构造测试用固定文本模型版本。"""
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id="llmprof_worker_test",
            name="worker-test",
            protocol="chat_completions",
            base_url="https://llm.example.test/v1",
            model="worker-test",
            has_api_key=True,
            context_window_tokens=8_192,
            tokenizer_encoding="o200k_base",
            supports_stream=False,
            supports_tools=False,
            max_output_tokens=4_096,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=NOW,
        ),
        api_key="test-only",
    )


class _Profiles:
    """始终返回同一固定模型版本的仓储替身。"""

    async def get_default_runtime(self) -> RuntimeModelProfile:
        """返回默认运行快照。"""
        return _runtime()

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
        """按固定版本 ID 返回同一测试快照。"""
        return _runtime() if profile_id == "llmprof_worker_test" else None


class _Extraction:
    """记录联合提取阶段并允许测试注入 Schema 失败。"""

    def __init__(
        self,
        *,
        schema_error: bool = False,
        decision_error: Exception | None = None,
    ) -> None:
        """保存测试开关并初始化调用计数。"""
        self.schema_error = schema_error
        self.decision_error = decision_error
        self.extraction_calls = 0
        self.decision_calls = 0
        self.sources: tuple[MemorySourceMessage, ...] = ()

    async def complete_extraction(
        self,
        batch: ExtractBatchPayload,
        runtime: RuntimeModelProfile,
        sources: tuple[MemorySourceMessage, ...],
    ) -> ExtractBatchPayload:
        """完成首轮提取或按测试开关抛出严格 Schema 错误。"""
        del runtime
        self.extraction_calls += 1
        self.sources = sources
        if self.schema_error:
            raise ExtractionSchemaError("memory_extraction_schema_invalid")
        return batch.model_copy(update={"extraction_complete": True})

    async def complete_decisions(
        self,
        batch: ExtractBatchPayload,
        runtime: RuntimeModelProfile,
        sources: tuple[MemorySourceMessage, ...],
    ) -> ExtractBatchPayload:
        """记录完整事实组决策调用并返回已完成批次。"""
        del runtime, sources
        self.decision_calls += 1
        if self.decision_error is not None:
            raise self.decision_error
        return batch.model_copy(update={"decisions_complete": True})

    async def prepare_changes(
        self,
        batch: ExtractBatchPayload,
        sources: tuple[MemorySourceMessage, ...],
    ) -> tuple[PreparedBatchChange, ...]:
        """返回空的已准备变更，由提交替身验证批次边界。"""
        del batch, sources
        return ()


class _Repository:
    """记录单任务领取、扫描、完成与原子批次提交调用。"""

    def __init__(self, job: MemoryJob | None) -> None:
        """保存最多领取一次的任务并初始化调用记录。"""
        self.job = job
        self.pending: tuple[MemoryPendingRun, ...] = ()
        self.pending_rounds: list[tuple[MemoryPendingRun, ...]] | None = None
        self.sources: tuple[MemorySourceMessage, ...] = ()
        self.claim_kinds: tuple[JobKind, ...] = ()
        self.claim_limit: int | None = None
        self.apply_commands: list[ApplyBatchCommand] = []
        self.completed: list[CompleteJobCommand] = []
        self.load_error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self.recovered = 0
        self.scan_page_limit: int | None = None
        self.maintenance_days: list[str] = []
        self.pending_calls = 0
        self.skipped: list[tuple[str, tuple[str, ...], str]] = []
        self.wake_calls = 0

    async def maintain_due_scopes(
        self,
        day: str,
        *,
        fact_max_age_days: int,
        other_max_age_days: int,
    ) -> int:
        """记录每日维护执行。"""
        del fact_max_age_days, other_max_age_days
        self.maintenance_days.append(day)
        return 0

    async def claim_job(
        self,
        *,
        kinds: tuple[JobKind, ...],
        limit: int = 1,
    ) -> MemoryJob | None:
        """只返回一次预设任务并记录固定领取参数。"""
        self.claim_kinds = kinds
        self.claim_limit = limit
        job = self.job
        self.job = None
        return job

    async def recover_running_jobs(self) -> int:
        """返回测试恢复数量。"""
        return self.recovered

    async def complete_job(self, command: CompleteJobCommand) -> bool:
        """记录非提取任务或真实失败收敛命令。"""
        self.completed.append(command)
        return True

    async def list_pending_runs(
        self,
        owner_id: str,
        *,
        limit: int,
        token_budget: int,
    ) -> tuple[MemoryPendingRun, ...]:
        """返回测试固定 pending Run，并校验固定用户范围。"""
        assert owner_id == "owner_worker"
        assert 1 <= limit <= 20
        assert token_budget >= 1_000
        if self.pending_rounds is not None:
            index = min(self.pending_calls, len(self.pending_rounds) - 1)
            self.pending_calls += 1
            return self.pending_rounds[index]
        return self.pending

    async def skip_runs(
        self,
        owner_id: str,
        run_ids: tuple[str, ...],
        safe_error_code: str,
    ) -> int:
        """记录仅针对不合规 Run 的跳过请求。"""
        self.skipped.append((owner_id, run_ids, safe_error_code))
        return len(run_ids)

    async def wake_user(self, owner_id: str) -> None:
        """记录仍有积压时的用户级重新调度。"""
        assert owner_id == "owner_worker"
        self.wake_calls += 1

    async def load_batch_sources(
        self,
        batch: ExtractBatchPayload,
    ) -> tuple[MemorySourceMessage, ...]:
        """返回固定来源或模拟 apply_batch 前的已知失效。"""
        del batch
        if self.load_error is not None:
            raise self.load_error
        return self.sources

    async def apply_batch(self, command: ApplyBatchCommand) -> ExtractBatchResult:
        """记录唯一原子提交命令并返回批次摘要。"""
        self.apply_commands.append(command)
        return ExtractBatchResult(
            batch_id=command.batch.batch_id,
            run_ids=tuple(item.run_id for item in command.batch.members),
            status=(
                "discarded"
                if command.discard_reason is not None
                else "applied"
            ),
            discard_reason=command.discard_reason,
        )

    async def scan_missing_runs(
        self,
        *,
        enabled_boundary_at: datetime,
        cursor_finished_at: datetime | None = None,
        cursor_run_id: str | None = None,
        page_limit: int = 100,
    ) -> ScanResult:
        """记录扫描页预算并返回完成页。"""
        del enabled_boundary_at, cursor_finished_at, cursor_run_id
        self.scan_page_limit = page_limit
        return ScanResult(round_complete=True)

    async def cleanup_scope(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> object:
        """按测试开关模拟 cleanup 临时异常。"""
        del namespace_id, scope_key
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return object()


def _message(message_id: str, text: str = "我喜欢苹果") -> MemorySourceMessage:
    """构造固定直接用户来源。"""
    return MemorySourceMessage(
        message_id=message_id,
        text=text,
        source_at=NOW,
    )


def _pending_run(
    run_id: str,
    *,
    project_id: str | None = None,
    messages: tuple[MemorySourceMessage, ...] | None = None,
) -> MemoryPendingRun:
    """构造带完整消息与固定上下文的 pending Run。"""
    return MemoryPendingRun(
        run_id=run_id,
        owner_id="owner_worker",
        session_id=f"session_{run_id}",
        project_id=project_id,
        finished_at=NOW,
        generation=7,
        messages=messages or (_message(f"msg_{run_id}"),),
        context_state=RunMemoryContextState(),
    )


def _job(
    payload: ExtractPendingPayload | CleanupScopePayload,
    *,
    attempt_count: int = 1,
    schema_retry_count: int = 0,
) -> MemoryJob:
    """构造普通 running 任务投影，不含租约字段。"""
    return MemoryJob(
        id=f"mjob_{payload.kind.value}",
        dedupe_key=f"{payload.kind.value}:owner_worker",
        kind=payload.kind,
        status=JobStatus.RUNNING,
        payload=payload,
        result=None,
        namespace_id="owner_worker",
        scope_key=None,
        attempt_count=attempt_count,
        schema_retry_count=schema_retry_count,
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _worker(
    repository: _Repository,
    extraction: _Extraction | None = None,
) -> MemoryWorker:
    """装配测试 Worker 并仅放宽协议替身类型。"""
    return MemoryWorker(
        cast(MemoryLifecycleRepositoryPort, repository),
        cast(ModelProfileRepository, _Profiles()),
        cast(MemoryExtractionService, extraction or _Extraction()),
        config=MemoryWorkerConfig(
            batch_run_limit=20,
            batch_token_limit=12_000,
            idle_sleep_seconds=0.05,
        ),
    )


def test_claims_one_background_job_and_excludes_control() -> None:
    """Worker 每次只领取一个普通任务，control 永远不在领取范围。"""

    async def scenario() -> None:
        repository = _Repository(
            _job(CleanupScopePayload(namespace_id="owner_worker", scope_key="project:p1"))
        )
        worker = _worker(repository)

        assert await worker.run_once() is True
        assert await worker.run_once() is False

        assert repository.claim_limit == 1
        assert repository.claim_kinds == BACKGROUND_JOB_KINDS
        assert JobKind.CONTROL not in repository.claim_kinds
        assert repository.scan_page_limit == 100
        assert len(repository.completed) == 1
        assert len(repository.maintenance_days) == 1

    asyncio.run(scenario())


def test_joint_extract_uses_complete_runs_and_apply_batch_owns_terminal_state() -> None:
    """联合批次保留两个 Run 的全部来源，apply_batch 后不再调用 complete_job。"""

    async def scenario() -> None:
        repository = _Repository(_job(ExtractPendingPayload(owner_id="owner_worker")))
        repository.pending = (
            _pending_run(
                "run_a",
                project_id="project_worker",
                messages=(_message("msg_a1"), _message("msg_a2")),
            ),
            _pending_run(
                "run_b",
                project_id="project_worker",
                messages=(_message("msg_b"),),
            ),
        )
        repository.sources = (
            _message("msg_a1"),
            _message("msg_a2"),
            _message("msg_b"),
        )
        extraction = _Extraction()

        assert await _worker(repository, extraction).run_once() is True

        assert extraction.extraction_calls == 1
        assert extraction.decision_calls == 1
        assert len(repository.apply_commands) == 1
        command = repository.apply_commands[0]
        assert command.job_id == "mjob_extract_pending"
        assert command.batch.generation == 7
        assert command.batch.model_profile_version_id == "llmprof_worker_test"
        assert command.batch.scope_keys == ("personal", "project:project_worker")
        assert tuple(
            source_id
            for member in command.batch.members
            for source_id in member.source_message_ids
        ) == ("msg_a1", "msg_a2", "msg_b")
        assert command.discard_reason is None
        assert repository.completed == []

    asyncio.run(scenario())


def test_scope_disabled_discards_atomically_without_redecision() -> None:
    """来源读取发现 scope 已禁用时只提交一次 discard，不重新提取。"""

    async def scenario() -> None:
        repository = _Repository(_job(ExtractPendingPayload(owner_id="owner_worker")))
        repository.pending = (_pending_run("run_changed"),)
        repository.load_error = ValueError("memory_scope_disabled")
        extraction = _Extraction()

        assert await _worker(repository, extraction).run_once() is True

        assert extraction.extraction_calls == 0
        assert extraction.decision_calls == 0
        assert repository.completed == []
        assert len(repository.apply_commands) == 1
        command = repository.apply_commands[0]
        assert command.changes == ()
        assert command.discard_reason == "memory_scope_disabled"
        assert tuple(item.run_id for item in command.batch.members) == (
            "run_changed",
        )

    asyncio.run(scenario())


def test_overlong_single_run_is_skipped_without_model_or_fragmentation() -> None:
    """单个完整 Run 超过预算时只跳过该 Run，不调用模型或构造片段。"""

    async def scenario() -> None:
        repository = _Repository(_job(ExtractPendingPayload(owner_id="owner_worker")))
        large_run = _pending_run(
            "run_large",
            messages=(_message("msg_large", "x" * 48_001),),
        )
        repository.pending_rounds = [(large_run,), ()]
        extraction = _Extraction()

        assert await _worker(repository, extraction).run_once() is True

        assert extraction.extraction_calls == 0
        assert extraction.decision_calls == 0
        assert repository.apply_commands == []
        assert repository.skipped == [
            ("owner_worker", ("run_large",), "memory_run_too_large")
        ]
        assert len(repository.completed) == 1

    asyncio.run(scenario())


def test_long_run_is_skipped_without_suppressing_valid_same_project_run() -> None:
    """101 条短消息的 Run 只跳过自身，同项目完整有效 Run 仍进入提取。"""

    async def scenario() -> None:
        repository = _Repository(_job(ExtractPendingPayload(owner_id="owner_worker")))
        long_run = _pending_run(
            "run_long",
            project_id="project_worker",
            messages=tuple(
                _message(f"msg_long_{index}", "x")
                for index in range(101)
            ),
        )
        valid_run = _pending_run(
            "run_valid",
            project_id="project_worker",
            messages=(_message("msg_valid_1"), _message("msg_valid_2")),
        )
        repository.pending_rounds = [(long_run, valid_run), (valid_run,)]
        repository.sources = (
            _message("msg_valid_1"),
            _message("msg_valid_2"),
        )
        extraction = _Extraction()

        assert await _worker(repository, extraction).run_once() is True

        assert repository.skipped == [
            ("owner_worker", ("run_long",), "memory_run_too_large")
        ]
        assert extraction.extraction_calls == 1
        assert extraction.decision_calls == 1
        assert len(repository.apply_commands) == 1
        command = repository.apply_commands[0]
        assert command.discard_reason is None
        assert tuple(item.run_id for item in command.batch.members) == (
            "run_valid",
        )
        assert command.batch.members[0].source_message_ids == (
            "msg_valid_1",
            "msg_valid_2",
        )

    asyncio.run(scenario())


def test_schema_failure_requests_one_bounded_retry() -> None:
    """首次 Schema 失败通过 complete_job 退避重试，而不是原子丢弃批次。"""

    async def scenario() -> None:
        repository = _Repository(
            _job(
                ExtractPendingPayload(owner_id="owner_worker"),
                schema_retry_count=0,
            )
        )
        repository.pending = (_pending_run("run_schema"),)
        repository.sources = (_message("msg_run_schema"),)

        assert await _worker(
            repository,
            _Extraction(schema_error=True),
        ).run_once() is True

        assert repository.apply_commands == []
        assert len(repository.completed) == 1
        command = repository.completed[0]
        assert command.retryable is True
        assert command.schema_invalid is True
        assert command.safe_error_code == "memory_extraction_schema_invalid"

    asyncio.run(scenario())


def test_temporary_cleanup_failure_is_retried_by_complete_job() -> None:
    """扫描、归档和清理的真实临时异常按普通任务状态有限退避。"""

    async def scenario() -> None:
        repository = _Repository(
            _job(
                CleanupScopePayload(
                    namespace_id="owner_worker",
                    scope_key="personal",
                ),
                attempt_count=2,
            )
        )
        repository.cleanup_error = RuntimeError("temporary")

        assert await _worker(repository).run_once() is True

        assert len(repository.completed) == 1
        command = repository.completed[0]
        assert command.retryable is True
        assert command.safe_error_code == "memory_worker_job_failed"
        assert command.result is None

    asyncio.run(scenario())


def test_recovery_and_config_contain_no_lease_or_parallel_worker_contract() -> None:
    """启动恢复通过新端口执行，配置不再暴露租约、心跳或并发字段。"""

    async def scenario() -> None:
        repository = _Repository(None)
        repository.recovered = 3
        worker = _worker(repository)

        assert await worker.recover_running_jobs() == 3
        assert not hasattr(worker._config, "lease_seconds")
        assert not hasattr(worker._config, "heartbeat_seconds")
        assert not hasattr(worker._config, "concurrency")

    asyncio.run(scenario())


def test_consumer_recovers_claimed_running_job_after_database_failure() -> None:
    """同一进程在领取后数据库收敛失败时，先恢复 running 再重新尝试。"""

    class _StopConsumption(RuntimeError):
        """用不可恢复异常结束测试消费循环。"""

    class _CrashingWorker:
        """模拟 claim 后终态写库暂时失败的单 Worker。"""

        def __init__(self) -> None:
            self.claimed = False
            self.recovered = False
            self.run_once_calls = 0
            self.events: list[str] = []

        async def run_once(self) -> bool:
            """首次模拟终态数据库失败，恢复后再次进入领取。"""
            self.run_once_calls += 1
            if self.run_once_calls == 1:
                self.claimed = True
                self.events.append("run_once.failed")
                raise OSError("database unavailable")
            assert self.recovered is True
            self.events.append("run_once.resumed")
            raise _StopConsumption

        async def recover_running_jobs(self) -> int:
            """只有 claim 后产生 running 时才执行恢复。"""
            assert self.claimed is True
            self.recovered = True
            self.events.append("recover")
            return 1

    async def scenario() -> None:
        import memory_worker as memory_entry_module

        worker = _CrashingWorker()

        async def sleep(seconds: float) -> None:
            """跳过真实退避等待并记录调用。"""
            assert seconds >= 0
            worker.events.append("sleep")

        try:
            await memory_entry_module._consume(
                cast(Any, worker),
                idle_sleep_seconds=0.05,
                sleep=sleep,
            )
        except _StopConsumption:
            pass
        else:
            raise AssertionError("consumer unexpectedly exited")

        assert worker.events == [
            "run_once.failed",
            "sleep",
            "recover",
            "run_once.resumed",
        ]

    asyncio.run(scenario())


def test_extract_final_failure_only_marks_batch_runs_failed() -> None:
    """提取任务最终失败时，仅把当前批次涉及的 run_id 传给 complete_job，不判死其他 pending Run。"""

    async def scenario() -> None:
        job = _job(ExtractPendingPayload(owner_id="owner_worker"), attempt_count=5)
        repository = _Repository(job)
        repository.pending = (_pending_run("run_batch_1"), _pending_run("run_batch_2"))
        extraction = _Extraction()
        extraction.decision_error = ExtractionInfrastructureError("model timeout")

        assert await _worker(repository, extraction).run_once() is True

        assert len(repository.completed) == 1
        command = repository.completed[0]
        assert command.retryable is False
        assert command.safe_error_code == "memory_extraction_model_failed"
        assert command.failed_run_ids == ("run_batch_1", "run_batch_2")

    asyncio.run(scenario())


def test_scan_missing_runs_is_throttled_and_does_not_break_backoff() -> None:
    """缺漏扫描受周期节流，且连续 run_once() 不会击穿失败任务的退避窗口。"""
    from datetime import timedelta

    class _BackoffRepository(_Repository):
        def __init__(self) -> None:
            super().__init__(None)
            self.now = NOW
            self.scan_calls = 0
            self.claim_count = 0
            self.job_status = JobStatus.QUEUED
            self.job_attempt = 0
            self.job_available_at = NOW
            self.round_complete_sequence: list[bool] = []

        async def scan_missing_runs(
            self,
            *,
            enabled_boundary_at: datetime,
            cursor_finished_at: datetime | None = None,
            cursor_run_id: str | None = None,
            page_limit: int = 100,
        ) -> ScanResult:
            del enabled_boundary_at, cursor_finished_at, cursor_run_id
            self.scan_calls += 1
            self.scan_page_limit = page_limit
            # 模拟 scan_missing_runs 的 respect_backoff=True 行为：不覆写 QUEUED 的退避时间。
            if self.job_status in {JobStatus.DONE, JobStatus.FAILED}:
                self.job_status = JobStatus.QUEUED
                self.job_attempt = 0
                self.job_available_at = self.now
            complete = (
                self.round_complete_sequence.pop(0) if self.round_complete_sequence else True
            )
            return ScanResult(
                scanned=1,
                awakened=1,
                has_more=not complete,
                next_cursor_finished_at=None if complete else self.now,
                next_cursor_run_id=None if complete else "run_cursor",
                round_complete=complete,
            )

        async def claim_job(
            self,
            *,
            kinds: tuple[JobKind, ...],
            limit: int = 1,
        ) -> MemoryJob | None:
            self.claim_kinds = kinds
            self.claim_limit = limit
            if self.job_status is not JobStatus.QUEUED or self.job_available_at > self.now:
                return None
            self.claim_count += 1
            self.job_status = JobStatus.RUNNING
            self.job_attempt += 1
            return _job(
                ExtractPendingPayload(owner_id="owner_worker"),
                attempt_count=self.job_attempt,
            )

        async def complete_job(self, command: CompleteJobCommand) -> bool:
            self.completed.append(command)
            if command.retryable and self.job_attempt < 5:
                self.job_status = JobStatus.QUEUED
                self.job_available_at = self.now + timedelta(seconds=10)
            else:
                self.job_status = JobStatus.FAILED
            return True

    async def scenario() -> None:
        clock = [100.0]
        repository = _BackoffRepository()
        repository.pending = (_pending_run("run_backoff"),)
        extraction = _Extraction(decision_error=ExtractionInfrastructureError("temporary"))
        worker = MemoryWorker(
            cast(MemoryLifecycleRepositoryPort, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(MemoryExtractionService, extraction),
            config=MemoryWorkerConfig(
                batch_run_limit=20,
                batch_token_limit=12_000,
                idle_sleep_seconds=0.05,
                scan_interval_seconds=30.0,
            ),
            monotonic=lambda: clock[0],
        )

        # 第 1 次 run_once：首次扫描立即触发，领取任务失败后进入 10 秒退避。
        assert await worker.run_once() is True
        assert repository.scan_calls == 1
        assert repository.claim_count == 1
        assert repository.job_available_at == NOW + timedelta(seconds=10)

        # 第 2、3 次 run_once（仍在退避与 30s 扫描间隔内）：不重复扫库，也不提前领取。
        clock[0] = 105.0
        repository.now = NOW + timedelta(seconds=5)
        assert await worker.run_once() is False
        assert await worker.run_once() is False
        assert repository.scan_calls == 1
        assert repository.claim_count == 1

        # 超过 30 秒触发新扫描，但若仍人为设置未来退避窗口，扫描也不得击穿退避。
        clock[0] = 131.0
        repository.job_available_at = NOW + timedelta(seconds=60)
        repository.now = NOW + timedelta(seconds=31)
        assert await worker.run_once() is False
        assert repository.scan_calls == 2
        assert repository.claim_count == 1
        assert repository.job_available_at == NOW + timedelta(seconds=60)

        # 多页扫描（round_complete=False）应立即续扫，不额外等待 30 秒。
        clock[0] = 162.0
        repository.round_complete_sequence = [False, True]
        assert await worker.run_once() is False
        assert repository.scan_calls == 3
        clock[0] = 162.5
        assert await worker.run_once() is False
        assert repository.scan_calls == 4
        # 整轮结束后重新进入 30 秒节流。
        clock[0] = 163.0
        assert await worker.run_once() is False
        assert repository.scan_calls == 4

    asyncio.run(scenario())


def test_optimistic_lock_target_changed_retries_instead_of_discarding_batch() -> None:
    """apply_batch 遇到 memory_batch_target_changed 乐观锁冲突时走可重试退避，不永久丢弃整批。"""

    class _TargetConflictRepository(_Repository):
        async def apply_batch(self, command: ApplyBatchCommand) -> ExtractBatchResult:
            if command.discard_reason is None:
                raise ValueError("memory_batch_target_changed")
            return await super().apply_batch(command)

    async def scenario() -> None:
        # 第 1 次尝试冲突 -> 应通过 complete_job(retryable=True) 退避重排，不调用 _discard_batch。
        repo_retry = _TargetConflictRepository(
            _job(ExtractPendingPayload(owner_id="owner_worker"), attempt_count=1)
        )
        repo_retry.pending = (_pending_run("run_lock_1"), _pending_run("run_lock_2"))
        repo_retry.sources = (_message("msg_run_lock_1"), _message("msg_run_lock_2"))
        assert await _worker(repo_retry).run_once() is True
        assert repo_retry.apply_commands == []
        assert len(repo_retry.completed) == 1
        assert repo_retry.completed[0].retryable is True
        assert repo_retry.completed[0].safe_error_code == "memory_batch_target_changed"
        assert repo_retry.completed[0].failed_run_ids == ()

        # 尝试耗尽（attempt_count=5） -> 收敛当前批次 Run 为失败，不再无限重试。
        repo_exhausted = _TargetConflictRepository(
            _job(ExtractPendingPayload(owner_id="owner_worker"), attempt_count=5)
        )
        repo_exhausted.pending = (_pending_run("run_lock_3"),)
        repo_exhausted.sources = (_message("msg_run_lock_3"),)
        assert await _worker(repo_exhausted).run_once() is True
        assert repo_exhausted.apply_commands == []
        assert len(repo_exhausted.completed) == 1
        assert repo_exhausted.completed[0].retryable is False
        assert repo_exhausted.completed[0].safe_error_code == "memory_batch_target_changed"
        assert repo_exhausted.completed[0].failed_run_ids == ("run_lock_3",)

    asyncio.run(scenario())
