"""Run Worker 生命周期边界。"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from run_worker import run_worker_process
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
    FakeSystemPromptRepository,
)

from app.run.config import RunWorkerConfig
from app.run.context import _run_context_work, prepare_run_tokenizers
from app.run.executor import RunExecutor as _RunExecutor
from app.run.worker import RunWorker
from domain.llm.model import LlmRequest, LlmStreamEvent, TextDelta
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.run.model import (
    FailRunCommand,
    Run,
    RunLease,
    RunLeaseState,
    RunModelSnapshot,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunRepository


def RunExecutor(
    repository: RunRepository,
    profiles: ModelProfileRepository,
    provider: LlmStreamProvider,
    **kwargs: Any,
) -> _RunExecutor:
    """为 Worker 单元测试注入固定系统提示词正文。"""
    return _RunExecutor(
        repository,
        profiles,
        provider,
        system_prompts=FakeSystemPromptRepository(),
        **kwargs,
    )


def _lease(status: RunStatus = RunStatus.RUNNING) -> RunLease:
    now = datetime.now(UTC)
    run = Run(
        id="run_worker_test",
        session_id="session_worker_test",
        owner_id="owner_worker_test",
        status=status,
        trigger_message_id="message_worker_test",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile_worker_test",
        model_snapshot=RunModelSnapshot(
            id="profile_worker_test",
            name="worker-test",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="test",
            context_window_tokens=8192,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=512,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=1,
        ),
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker-cleanup-0",
        lease_expires_at=now + timedelta(seconds=3),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(seconds=30),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    return RunLease(
        run=run,
        lease_expires_at=now + timedelta(seconds=3),
        execution_deadline_at=now + timedelta(seconds=30),
    )


class _CleanupRepository:
    def __init__(self) -> None:
        self.lease = _lease()
        self.claimed = False
        self.heartbeats = 0
        self.fourth_heartbeat = asyncio.Event()
        self.settled = False
        self.settled_event = asyncio.Event()

    async def claim(self, worker_id: str, **kwargs: object) -> tuple[RunLease, ...]:
        if self.claimed:
            return ()
        self.claimed = True
        self.lease = RunLease(
            run=replace(self.lease.run, worker_id=worker_id),
            lease_expires_at=self.lease.lease_expires_at,
            execution_deadline_at=self.lease.execution_deadline_at,
        )
        return (self.lease,)

    async def heartbeat(self, *args: object, **kwargs: object) -> Run:
        self.heartbeats += 1
        if self.heartbeats >= 4:
            self.fourth_heartbeat.set()
        return self.lease.run

    async def get(self, *args: object, **kwargs: object) -> Run:
        return replace(self.lease.run, status=RunStatus.CANCELLING)

    async def get_lease_state(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
    ) -> RunLeaseState | None:
        run = await self.get(session_id, owner_id, run_id)
        if run is None:
            return None
        return RunLeaseState(
            status=run.status,
            worker_id=run.worker_id,
            execution_epoch=run.execution_epoch,
        )

    async def settle_cancelled(self, *args: object, **kwargs: object) -> Run:
        self.settled = True
        self.settled_event.set()
        return self.lease.run

    async def reclaim_expired(self, **kwargs: object) -> tuple[Run, ...]:
        return ()


class _SlowCleanupExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_received = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    async def execute(self, lease: RunLease) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_received.set()
            await self.cleanup_release.wait()
            raise

    async def wait_background_tasks(self) -> None:
        return None


class _FinishedExecutor:
    async def execute(self, lease: RunLease) -> None:
        return None

    async def wait_background_tasks(self) -> None:
        return None


def test_worker_continues_claiming_after_one_execution_task_raises() -> None:
    """单个执行协程抛错时收敛当前 Run，consumer 继续处理后续任务。"""

    async def scenario() -> None:
        first = _lease()
        second = replace(
            first,
            run=replace(first.run, id="run_worker_second"),
        )

        class Repository(_CleanupRepository):
            def __init__(self) -> None:
                super().__init__()
                self.claim_count = 0
                self.states = {
                    first.run.id: RunStatus.RUNNING,
                    second.run.id: RunStatus.RUNNING,
                }
                self.failed_runs: list[str] = []
                self.owners: dict[str, str] = {}

            async def claim(self, worker_id: str, **kwargs: object) -> tuple[RunLease, ...]:
                self.claim_count += 1
                if self.claim_count == 1:
                    lease = first
                elif self.claim_count == 2:
                    lease = second
                else:
                    return ()
                lease = replace(lease, run=replace(lease.run, worker_id=worker_id))
                self.owners[lease.run.id] = worker_id
                return (lease,)

            async def get(self, *args: object, **kwargs: object) -> Run:
                run_id = cast(str, args[2])
                target = first if run_id == first.run.id else second
                return replace(
                    target.run,
                    worker_id=self.owners[run_id],
                    status=self.states[run_id],
                )

            async def fail(self, command: FailRunCommand) -> Run:
                run_id = command.run_id
                self.states[run_id] = RunStatus.FAILED
                self.failed_runs.append(run_id)
                return replace(first.run, status=RunStatus.FAILED)

        repository = Repository()
        second_completed = asyncio.Event()

        class Executor:
            async def execute(self, lease: RunLease) -> None:
                if lease.run.id == first.run.id:
                    raise RuntimeError("injected_worker_task_failure")
                repository.states[lease.run.id] = RunStatus.COMPLETED
                second_completed.set()

        worker = RunWorker(
            cast(RunRepository, repository),
            cast(RunExecutor, Executor()),
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.05,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(second_completed.wait(), timeout=2)
        finally:
            await worker.stop()
            await asyncio.wait_for(task, timeout=2)
        assert repository.claim_count >= 2
        assert repository.failed_runs == [first.run.id]

    asyncio.run(scenario())


def test_executor_preserves_cancellation_when_usage_write_is_rejected() -> None:
    """usage 记账失租不能把原始取消改写为普通执行错误。"""

    async def scenario() -> None:
        started = asyncio.Event()
        usage_attempted = asyncio.Event()

        class Repository:
            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *args: object) -> tuple[object, ...]:
                return ()

            async def list_successfully_loaded_skill_ids(self, _run_id: str) -> tuple[str, ...]:
                return ()

            async def list_tool_calls(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def record_usage(self, command: object) -> None:
                usage_attempted.set()
                return None

            async def fail(self, command: object) -> None:
                return None

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                now = datetime.now(UTC)
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=512,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=60,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                async def events() -> AsyncIterator[LlmStreamEvent]:
                    started.set()
                    await asyncio.Event().wait()
                    yield TextDelta(0, 0, "never emitted")

                yield events()

        task = asyncio.create_task(
            RunExecutor(
                cast(RunRepository, Repository()),
                cast(ModelProfileRepository, Profiles()),
                cast(LlmStreamProvider, Provider()),
            ).execute(_lease())
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert usage_attempted.is_set()

    asyncio.run(scenario())


def test_executor_deadline_cleanup_survives_unwritable_usage_ledger() -> None:
    """deadline 取消流后即使用量因失租不可写也正常记录超时收尾。"""

    async def scenario() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        usage_attempted = asyncio.Event()
        failed = asyncio.Event()

        class Repository:
            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *args: object) -> tuple[object, ...]:
                return ()

            async def list_successfully_loaded_skill_ids(self, _run_id: str) -> tuple[str, ...]:
                return ()

            async def list_tool_calls(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def record_usage(self, command: object) -> None:
                usage_attempted.set()
                return None

            async def fail(self, command: object) -> None:
                failed.set()
                return None

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                now = datetime.now(UTC)
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=512,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=1,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                async def events() -> AsyncIterator[LlmStreamEvent]:
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()
                    yield TextDelta(0, 0, "never emitted")

                yield events()

        base_lease = _lease()
        short_deadline = datetime.now(UTC) + timedelta(seconds=1)
        deadline_lease = replace(base_lease, execution_deadline_at=short_deadline)
        executor = RunExecutor(
            cast(RunRepository, Repository()),
            cast(ModelProfileRepository, Profiles()),
            cast(LlmStreamProvider, Provider()),
        )
        await executor.execute(deadline_lease)
        assert started.is_set()
        assert cleaned.is_set()
        assert usage_attempted.is_set()
        assert failed.is_set()

    asyncio.run(scenario())


def test_worker_waits_for_cancel_cleanup_without_renewing_lease() -> None:
    """取消清理阶段限时等待 Executor 退出而不启动续租循环，清理结束后再落 cancelled。"""

    async def scenario() -> None:
        repository = _CleanupRepository()
        executor = _SlowCleanupExecutor()
        worker = RunWorker(
            cast(RunRepository, repository),
            cast(RunExecutor, executor),
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.05,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )
        worker_task = asyncio.create_task(worker.run())
        await executor.started.wait()
        try:
            await asyncio.wait_for(executor.cancel_received.wait(), timeout=2.0)
            await asyncio.sleep(0.15)
            assert not repository.settled
            assert repository.heartbeats == 0
        finally:
            executor.cleanup_release.set()
            await worker.stop()
            await asyncio.wait_for(worker_task, timeout=2)
        assert repository.heartbeats == 0
        assert repository.settled

    asyncio.run(scenario())


def test_worker_cancel_and_join_bounds_stuck_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """清理协程长时间不返回时按限时退出并告警，不无限阻塞 Worker 槽位。"""
    import app.run.worker as worker_module

    monkeypatch.setattr(worker_module, "_CANCEL_CLEANUP_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        release = asyncio.Event()

        async def stuck_cleanup() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        worker = RunWorker(
            cast(RunRepository, _CleanupRepository()),
            cast(RunExecutor, _FinishedExecutor()),
            RunWorkerConfig(),
        )
        task = asyncio.create_task(stuck_cleanup())
        await asyncio.sleep(0)
        with caplog.at_level("WARNING"):
            await asyncio.wait_for(worker._cancel_and_join(task, _lease()), timeout=1.0)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert any("run_execution_cleanup_timed_out" in record.message for record in caplog.records)

    asyncio.run(scenario())


def test_worker_settles_cancel_committed_before_executor_returns() -> None:
    """执行协程已完成时仍复核 cancelling，清理后立即写 cancelled。"""

    async def scenario() -> None:
        repository = _CleanupRepository()
        worker = RunWorker(
            cast(RunRepository, repository),
            cast(RunExecutor, _FinishedExecutor()),
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.05,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(repository.settled_event.wait(), timeout=2)
        finally:
            await worker.stop()
            await asyncio.wait_for(task, timeout=2)
        assert repository.settled

    asyncio.run(scenario())


def test_worker_entry_propagates_fatal_task_error_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 启动后立即失败时入口报错，并释放 Provider、存储、查询服务与数据库。"""
    import minio

    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.internal:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "ak")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "sk")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")

    class _FakeMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def bucket_exists(self, _bucket: str) -> bool:
            return True

    monkeypatch.setattr(minio, "Minio", _FakeMinio)

    async def scenario() -> None:
        state = SimpleNamespace(provider_closed=False, database_closed=False, stopped=False)

        class FakeDatabase:
            session_factory = object()

            def __init__(self, database_url: str, **_kwargs: Any) -> None:
                pass

            async def dispose(self) -> None:
                state.database_closed = True

        class FakeProvider:
            async def aclose(self) -> None:
                state.provider_closed = True

        class FakeWorker:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def run(self) -> None:
                raise RuntimeError("worker_fatal")

            async def stop(self) -> None:
                state.stopped = True

        with (
            patch("run_worker.prepare_run_tokenizers", _async_noop),
            patch("bootstrap.run_worker.McpSecretCipher.from_environment", return_value=object()),
            patch("bootstrap.database.Database", FakeDatabase),
            patch("bootstrap.run_worker.SqlAlchemyModelProfileRepository", lambda *_: object()),
            patch("bootstrap.run_worker.SqlAlchemyMessageRepository", lambda _: object()),
            patch("bootstrap.run_worker.SqlAlchemyRunRepository", lambda *_: object()),
            patch("bootstrap.run_worker.OpenAIChatCompletionsAdapter", FakeProvider),
            patch("bootstrap.run_worker.RunExecutor", lambda *_args, **_kwargs: object()),
            patch("bootstrap.run_worker.RunWorker", FakeWorker),
        ):
            with pytest.raises(RuntimeError, match="worker_fatal"):
                await run_worker_process(database_url="postgresql://unused")

        assert state.stopped
        assert state.provider_closed
        assert state.database_closed

    asyncio.run(scenario())


async def _async_noop() -> None:
    return None


def test_slow_tokenizer_work_times_out_without_blocking_other_lease_heartbeats() -> None:
    """冷 tokenizer/count 工作超时后事件循环仍可续约其他 Run。"""

    async def scenario() -> None:
        heartbeats = 0
        stop = asyncio.Event()

        async def renew_other_run() -> None:
            nonlocal heartbeats
            while not stop.is_set():
                heartbeats += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(renew_other_run())
        try:
            with pytest.raises(TimeoutError):
                await _run_context_work(time.sleep, 0.15, timeout_seconds=0.02)
            assert heartbeats >= 2
        finally:
            stop.set()
            await heartbeat_task

    asyncio.run(scenario())


def test_tokenizer_startup_preparation_has_a_hard_async_timeout() -> None:
    """冷缓存下载超时会中止 Worker 启动且不阻塞事件循环。"""

    async def scenario() -> None:
        heartbeat = asyncio.Event()

        async def tick() -> None:
            await asyncio.sleep(0.005)
            heartbeat.set()

        with patch("app.run.context._load_supported_tokenizers", lambda: time.sleep(0.15)):
            ticker = asyncio.create_task(tick())
            with pytest.raises(TimeoutError):
                await prepare_run_tokenizers(timeout_seconds=0.02)
            await ticker
        assert heartbeat.is_set()

    asyncio.run(scenario())


def test_tokenizer_calls_wait_for_bounded_worker_capacity() -> None:
    """达到线程并发上限时排队等待，不立即失败或扩张线程数。"""

    async def scenario() -> None:
        await asyncio.gather(
            *(_run_context_work(time.sleep, 0.03, timeout_seconds=1) for _ in range(5))
        )

    asyncio.run(scenario())


def test_late_executor_failure_after_timeout_produces_no_loop_errors() -> None:
    """超时后线程池任务延迟失败时，不会向事件循环泄漏未捕获异常。"""

    async def scenario() -> None:
        gate = threading.Event()
        thread_finished = threading.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        def blocked_work() -> None:
            try:
                gate.wait()
                raise ValueError("late_tokenizer_failure")
            finally:
                thread_finished.set()

        with pytest.raises(TimeoutError):
            await _run_context_work(blocked_work, timeout_seconds=0.01)

        gate.set()
        deadline = asyncio.get_running_loop().time() + 1
        while not thread_finished.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert thread_finished.is_set()
        await asyncio.sleep(0)
        assert loop_errors == []

    asyncio.run(scenario())


def test_executor_does_not_start_provider_after_context_work_deadline() -> None:
    """上下文预算工作超时后 Run 失败且不发起模型调用。"""

    async def scenario() -> None:
        failed = asyncio.Event()
        provider_called = False

        class Repository:
            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def list_context_messages(self, *_args: object) -> tuple[object, ...]:
                return ()

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def fail(self, *_args: object, **_kwargs: object) -> None:
                failed.set()

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                now = datetime.now(UTC)
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=512,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=1,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, _runtime: RuntimeModelProfile, _request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                nonlocal provider_called
                provider_called = True
                yield _empty_events()

        async def slow_context_count(*_args: object, **_kwargs: object) -> object:
            await asyncio.sleep(1)
            return (), (), 1, 1

        with patch("app.run.executor.measure_llm_request", slow_context_count):
            lease = _lease()
            deadline = datetime.now(UTC) + timedelta(milliseconds=30)
            await RunExecutor(
                cast(RunRepository, Repository()),
                cast(ModelProfileRepository, Profiles()),
                cast(LlmStreamProvider, Provider()),
            ).execute(replace(lease, execution_deadline_at=deadline))

        assert failed.is_set()
        assert not provider_called

    async def _empty_events() -> AsyncIterator[LlmStreamEvent]:
        if False:
            yield TextDelta(0, 0, "")

    asyncio.run(scenario())


def test_worker_isolates_transient_db_error_without_stopping_sibling_consumer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """单个 consumer 遇到瞬态数据库错误只做局部退避，不拖垮同进程并发在途的 Run。"""
    from sqlalchemy.exc import OperationalError

    async def scenario() -> None:
        lease = _lease()
        in_flight_started = asyncio.Event()
        in_flight_release = asyncio.Event()
        in_flight_finished = asyncio.Event()
        sibling_recovered = asyncio.Event()

        class Repository(_CleanupRepository):
            def __init__(self) -> None:
                super().__init__()
                self.claim_calls: dict[str, int] = {}
                self.status = RunStatus.RUNNING
                self.owner_worker: str | None = None

            async def claim(self, worker_id: str, **kwargs: object) -> tuple[RunLease, ...]:
                del kwargs
                call_no = self.claim_calls.get(worker_id, 0) + 1
                self.claim_calls[worker_id] = call_no
                if worker_id.endswith("-0") and call_no == 1:
                    self.owner_worker = worker_id
                    return (
                        replace(
                            lease,
                            run=replace(lease.run, worker_id=worker_id),
                        ),
                    )
                if worker_id.endswith("-1"):
                    if call_no <= 2:
                        raise OperationalError(
                            "SELECT 1",
                            {},
                            Exception("connection reset by peer"),
                        )
                    sibling_recovered.set()
                return ()

            async def get(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                return replace(
                    lease.run,
                    worker_id=self.owner_worker,
                    status=self.status,
                )

            async def heartbeat(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                return replace(lease.run, worker_id=self.owner_worker)

        repository = Repository()

        class Executor:
            async def execute(self, active_lease: RunLease) -> None:
                del active_lease
                in_flight_started.set()
                await in_flight_release.wait()
                repository.status = RunStatus.COMPLETED
                in_flight_finished.set()

        worker = RunWorker(
            cast(RunRepository, repository),
            cast(RunExecutor, Executor()),
            RunWorkerConfig(
                concurrency=2,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.05,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
            worker_id="worker-resilient",
        )
        with caplog.at_level("WARNING"):
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(in_flight_started.wait(), timeout=2)
                await asyncio.wait_for(sibling_recovered.wait(), timeout=5)
                # 兄弟 consumer 触发 2 次瞬态 DB 异常并恢复期间，在途 Run 未被取消。
                in_flight_release.set()
                await asyncio.wait_for(in_flight_finished.wait(), timeout=2)
            finally:
                await worker.stop()
                await asyncio.wait_for(task, timeout=2)

        assert repository.status is RunStatus.COMPLETED
        assert repository.claim_calls["worker-resilient-1"] >= 3
        retry_logs = [
            record.message
            for record in caplog.records
            if "run_worker_database_retry" in record.message
        ]
        assert len(retry_logs) == 2

    asyncio.run(scenario())


def test_worker_propagates_non_recoverable_error_in_consumer() -> None:
    """非瞬态编程错误仍按致命异常抛出，不静默吞错。"""

    async def scenario() -> None:
        class BrokenRepository(_CleanupRepository):
            async def claim(self, worker_id: str, **kwargs: object) -> tuple[RunLease, ...]:
                del worker_id, kwargs
                raise ValueError("unexpected_programming_bug")

        worker = RunWorker(
            cast(RunRepository, BrokenRepository()),
            cast(RunExecutor, _FinishedExecutor()),
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.05,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )
        with pytest.raises(ExceptionGroup) as exc_info:
            await worker.run()
        assert any(isinstance(err, ValueError) for err in exc_info.value.exceptions)

    asyncio.run(scenario())


def test_worker_finalize_publishes_events_through_failed_run_sequence() -> None:
    """兜底 fail() 推进 event_sequence 后，必须使用 failed_run.event_sequence 推送终态事件。"""
    from domain.run.model import RunEvent, RunFailedPayload
    from domain.run.ports import RunEventTransport

    async def scenario() -> None:
        base_lease = _lease()
        # 领取时 event_sequence=2，崩溃前未写入新事件；若仍用旧 current.event_sequence=2
        # 作为目标序号，publish_pending_events 会直接短路漏推 fail() 新增的 sequence=3 事件。
        lease = replace(base_lease, run=replace(base_lease.run, event_sequence=2))
        failed_event = RunEvent(
            id="evt_failed_3",
            run_id=lease.run.id,
            sequence=3,
            type="failed",
            created_at=datetime.now(UTC),
            payload=RunFailedPayload(safe_error_code="execution_failed"),
        )
        published_sequences: list[int] = []
        queried_limits: list[tuple[int, int]] = []

        class Repository:
            async def get(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                # 兜底前 event_sequence 为 2，fail() 写入新事件后推进至 3。
                return replace(lease.run, status=RunStatus.RUNNING, event_sequence=2)

            async def fail(self, command: FailRunCommand) -> Run:
                assert command.safe_error_code == "execution_failed"
                return replace(lease.run, status=RunStatus.FAILED, event_sequence=3)

            async def list_events(
                self,
                session_id: str,
                owner_id: str,
                run_id: str,
                *,
                after_sequence: int,
                limit: int,
                through_sequence: int | None = None,
            ) -> tuple[RunEvent, ...]:
                del session_id, owner_id, run_id, through_sequence
                queried_limits.append((after_sequence, limit))
                if after_sequence < failed_event.sequence:
                    return (failed_event,)
                return ()

        class Publisher:
            async def publish_persisted(
                self,
                run_id: str,
                execution_epoch: int,
                event: RunEvent,
            ) -> None:
                del run_id, execution_epoch
                published_sequences.append(event.sequence)

        worker = RunWorker(
            cast(RunRepository, Repository()),
            cast(RunExecutor, _FinishedExecutor()),
            RunWorkerConfig(),
            publisher=cast(RunEventTransport, Publisher()),
        )

        async def crashing_execution() -> None:
            raise RuntimeError("boom")

        task = asyncio.create_task(crashing_execution())
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.gather(task)
        await worker._finalize_execution(lease, task)

        assert queried_limits == [(2, 1)]
        assert published_sequences == [3]

    asyncio.run(scenario())


def test_worker_drive_lease_cleanup_exception_does_not_crash_sibling_consumer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """瞬态 DB 异常触发 _cancel_and_join 时，清理阶段抛错须被收敛且不击穿兄弟 consumer。"""
    from sqlalchemy.exc import OperationalError

    async def scenario() -> None:
        failing_lease = _lease()
        sibling_lease = replace(
            failing_lease,
            run=replace(failing_lease.run, id="run_worker_sibling"),
        )
        sibling_started = asyncio.Event()
        sibling_release = asyncio.Event()
        sibling_finished = asyncio.Event()
        cleanup_logged = asyncio.Event()

        class Repository(_CleanupRepository):
            def __init__(self) -> None:
                super().__init__()
                self.claimed_by_worker: dict[str, int] = {}
                self.owners: dict[str, str] = {}
                self.states = {
                    failing_lease.run.id: RunStatus.RUNNING,
                    sibling_lease.run.id: RunStatus.RUNNING,
                }
                self.get_calls: dict[str, int] = {}

            async def claim(self, worker_id: str, **kwargs: object) -> tuple[RunLease, ...]:
                del kwargs
                count = self.claimed_by_worker.get(worker_id, 0) + 1
                self.claimed_by_worker[worker_id] = count
                if worker_id.endswith("-0") and count == 1:
                    self.owners[failing_lease.run.id] = worker_id
                    return (
                        replace(
                            failing_lease,
                            run=replace(failing_lease.run, worker_id=worker_id),
                        ),
                    )
                if worker_id.endswith("-1") and count == 1:
                    self.owners[sibling_lease.run.id] = worker_id
                    return (
                        replace(
                            sibling_lease,
                            run=replace(sibling_lease.run, worker_id=worker_id),
                        ),
                    )
                return ()

            async def get(self, *args: object, **kwargs: object) -> Run:
                del kwargs
                run_id = cast(str, args[2])
                self.get_calls[run_id] = self.get_calls.get(run_id, 0) + 1
                if run_id == failing_lease.run.id:
                    raise OperationalError("SELECT 1", {}, Exception("db connection dropped"))
                return replace(
                    sibling_lease.run,
                    worker_id=self.owners.get(run_id),
                    status=self.states[run_id],
                )

            async def heartbeat(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                return sibling_lease.run

        repository = Repository()

        class Executor:
            async def execute(self, active_lease: RunLease) -> None:
                if active_lease.run.id == failing_lease.run.id:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup_logged.set()
                        raise RuntimeError("cleanup_boom")
                sibling_started.set()
                await sibling_release.wait()
                repository.states[sibling_lease.run.id] = RunStatus.COMPLETED
                sibling_finished.set()

        worker = RunWorker(
            cast(RunRepository, repository),
            cast(RunExecutor, Executor()),
            RunWorkerConfig(
                concurrency=2,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.02,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
            worker_id="worker-cleanup-guard",
        )
        with caplog.at_level("WARNING"):
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(sibling_started.wait(), timeout=2)
                await asyncio.wait_for(cleanup_logged.wait(), timeout=2)
                # 等待短暂时间确认兄弟 consumer 未因 cleanup_boom 被 TaskGroup 取消。
                await asyncio.sleep(0.05)
                sibling_release.set()
                await asyncio.wait_for(sibling_finished.wait(), timeout=2)
            finally:
                await worker.stop()
                await asyncio.wait_for(task, timeout=2)

        assert repository.states[sibling_lease.run.id] is RunStatus.COMPLETED
        assert any("run_execution_cleanup_failed" in record.message for record in caplog.records)
        assert any("run_worker_drive_database_retry" in record.message for record in caplog.records)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("poll_state", "expect_settled"),
    [
        ("cancelling", True),
        ("lost_worker", False),
        ("lost_epoch", False),
        ("deleted", False),
    ],
)
def test_worker_drive_lease_prefers_lightweight_get_lease_state_for_poll_branches(
    poll_state: str,
    expect_settled: bool,
) -> None:
    """1s 取消轮询应优先走轻量 get_lease_state，正确处理取消、失租与删除分支且不调用完整 get。"""

    async def scenario() -> None:
        lease = _lease()
        execution_cancelled = asyncio.Event()

        class LightweightRepo:
            def __init__(self) -> None:
                self.lease_state_calls = 0
                self.get_calls = 0
                self.settle_calls = 0
                self.fail_calls = 0

            async def get_lease_state(
                self, session_id: str, owner_id: str, run_id: str
            ) -> RunLeaseState | None:
                assert (session_id, owner_id, run_id) == (
                    lease.run.session_id,
                    lease.run.owner_id,
                    lease.run.id,
                )
                self.lease_state_calls += 1
                if self.lease_state_calls == 1:
                    return RunLeaseState(
                        status=RunStatus.RUNNING,
                        worker_id=lease.run.worker_id,
                        execution_epoch=lease.run.execution_epoch,
                    )
                if poll_state == "cancelling":
                    return RunLeaseState(
                        status=RunStatus.CANCELLING,
                        worker_id=lease.run.worker_id,
                        execution_epoch=lease.run.execution_epoch,
                    )
                if poll_state == "lost_worker":
                    return RunLeaseState(
                        status=RunStatus.RUNNING,
                        worker_id="other-worker",
                        execution_epoch=lease.run.execution_epoch,
                    )
                if poll_state == "lost_epoch":
                    return RunLeaseState(
                        status=RunStatus.RUNNING,
                        worker_id=lease.run.worker_id,
                        execution_epoch=lease.run.execution_epoch + 1,
                    )
                return None

            async def get(self, *args: object, **kwargs: object) -> Run | None:
                del args, kwargs
                self.get_calls += 1
                raise AssertionError("poll loop should use get_lease_state instead of full get")

            async def heartbeat(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                return lease.run

            async def settle_cancelled(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                self.settle_calls += 1
                return replace(lease.run, status=RunStatus.CANCELLED)

            async def fail(self, *args: object, **kwargs: object) -> Run:
                del args, kwargs
                self.fail_calls += 1
                return replace(lease.run, status=RunStatus.FAILED)

        class HangingExecutor:
            async def execute(self, active_lease: RunLease) -> None:
                del active_lease
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    execution_cancelled.set()
                    raise

        repo = LightweightRepo()
        worker = RunWorker(
            cast(RunRepository, repo),
            cast(_RunExecutor, HangingExecutor()),
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.01,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )
        await asyncio.wait_for(worker._drive_lease(lease), timeout=2.0)
        assert execution_cancelled.is_set()
        assert repo.lease_state_calls >= 2
        assert repo.get_calls == 0
        assert repo.fail_calls == 0
        assert repo.settle_calls == (1 if expect_settled else 0)

    asyncio.run(scenario())


def test_sqlalchemy_run_repository_get_lease_state_projects_scalars_and_enforces_scope() -> None:
    """SqlAlchemyRunRepository.get_lease_state 只投影三标量列，并严格隔离 owner 与软删会话。"""
    from pg_unit_harness import create_pg_unit_db
    from sqlalchemy import event

    from adapter.auth.model import UserModel
    from adapter.llm.model import ModelProfileVersionModel
    from adapter.message.model import ConversationMessageModel
    from adapter.message.repository import SqlAlchemyMessageRepository
    from adapter.run.model import RunModel
    from adapter.run.repository import SqlAlchemyRunRepository
    from adapter.session.model import SessionModel
    from adapter.system_prompt.model import SystemPromptVersionModel

    async def scenario() -> None:
        engine, factory = await create_pg_unit_db()
        executed_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _params: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            executed_sql.append(statement)

        try:
            now = datetime.now(UTC)
            async with factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id="usr_lease",
                            email="lease@example.test",
                            password_hash="hash",
                            display_name="Lease User",
                            status="active",
                            role="member",
                            auth_provider="local",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.add(
                        ModelProfileVersionModel(
                            id="mpv_lease",
                            name="lease-model",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="gpt-test",
                            api_key_ciphertext=b"ciphertext",
                            api_key_nonce=b"0123456789ab",
                            api_key_key_version=1,
                            context_window_tokens=8192,
                            tokenizer_encoding="o200k_base",
                            supports_stream=True,
                            supports_tools=True,
                            supports_parallel_tool_calls=True,
                            max_output_tokens=512,
                            request_timeout_seconds=30,
                            execution_timeout_seconds=60,
                            max_concurrency=1,
                            enabled=True,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.add_all(
                        [
                            SystemPromptVersionModel(
                                id="spv_main_lease",
                                kind="main",
                                content="main prompt",
                                created_at=now,
                            ),
                            SystemPromptVersionModel(
                                id="spv_comp_lease",
                                kind="compression",
                                content="comp prompt",
                                created_at=now,
                            ),
                        ]
                    )
                    await session.flush()
                    session.add(
                        SessionModel(
                            id="ses_lease",
                            owner_id="usr_lease",
                            title="lease session",
                            last_message_sequence=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                    session.add(
                        ConversationMessageModel(
                            id="msg_lease_1",
                            session_id="ses_lease",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "hi"}],
                            run_id="run_lease_1",
                            context_turn_id="msg_lease_1",
                            created_at=now,
                            metadata_json={},
                        )
                    )
                    session.add(
                        RunModel(
                            id="run_lease_1",
                            session_id="ses_lease",
                            owner_id="usr_lease",
                            status=RunStatus.RUNNING.value,
                            trigger_message_id="msg_lease_1",
                            context_cutoff_sequence=1,
                            model_config_version_id="mpv_lease",
                            model_snapshot_json={
                                "id": "mpv_lease",
                                "name": "lease-model",
                                "protocol": "chat_completions",
                                "base_url": "https://llm.example.test/v1",
                                "model": "gpt-test",
                                "context_window_tokens": 8192,
                                "tokenizer_encoding": "o200k_base",
                                "supports_stream": True,
                                "supports_tools": True,
                                "supports_parallel_tool_calls": True,
                                "max_output_tokens": 512,
                                "request_timeout_seconds": 30,
                                "execution_timeout_seconds": 60,
                                "max_concurrency": 1,
                                "temperature": None,
                                "scenario_label": None,
                                "supports_vision": False,
                                "version": 1,
                            },
                            main_prompt_version_id="spv_main_lease",
                            compression_prompt_version_id="spv_comp_lease",
                            prompt_template_version="run-v2",
                            event_sequence=1,
                            execution_epoch=3,
                            worker_id="worker-light-0",
                            queued_at=now,
                            created_at=now,
                            updated_at=now,
                            usage={},
                            tool_snapshot={"tools": []},
                        )
                    )

            repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            executed_sql.clear()

            state = await repo.get_lease_state("ses_lease", "usr_lease", "run_lease_1")
            assert state == RunLeaseState(
                status=RunStatus.RUNNING,
                worker_id="worker-light-0",
                execution_epoch=3,
            )
            # 确认只执行 1 条标量 SELECT，不拉取大 JSONB 快照且不查 run_input_requests。
            assert len(executed_sql) == 1
            sql = executed_sql[0]
            assert "run_input_requests" not in sql
            assert "tool_snapshot" not in sql
            assert "skill_snapshot" not in sql
            assert "model_snapshot_json" not in sql
            assert "usage" not in sql

            # 非 owner 不可见。
            assert await repo.get_lease_state("ses_lease", "other_user", "run_lease_1") is None

            # 会话软删后返回 None。
            async with factory() as session:
                async with session.begin():
                    ses = await session.get(SessionModel, "ses_lease")
                    assert ses is not None
                    ses.deleted_time = now
            assert await repo.get_lease_state("ses_lease", "usr_lease", "run_lease_1") is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _runtime_profile(profile_id: str) -> RuntimeModelProfile:
    now = datetime.now(UTC)
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id=profile_id,
            name="worker-budget-test",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="test",
            context_window_tokens=8192,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=512,
            request_timeout_seconds=30,
            execution_timeout_seconds=30,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        api_key="test-secret",
    )


def _user_trigger_message(run: Run) -> Any:
    from domain.message import (
        ConversationMessage,
        ConversationRole,
        MessageMetadata,
        MessageStatus,
        TextPart,
    )

    return ConversationMessage(
        id=run.trigger_message_id,
        session_id=run.session_id,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        sequence=1,
        parts=(TextPart(text="hello"),),
        reply_to_message_id=None,
        run_id=run.id,
        context_turn_id=run.trigger_message_id,
        client_message_id=None,
        created_at=run.created_at,
        completed_at=run.created_at,
        metadata=MessageMetadata(),
    )


def test_worker_cancelling_in_flight_llm_forwards_llm_request_count_to_settle_cancelled() -> None:
    """在途 LLM 流式请求被取消导致 record_usage 返回 None 时，settle_cancelled 仍须落库请求计数。"""

    async def scenario() -> None:
        base_lease = _lease()
        lease = replace(
            base_lease,
            run=replace(base_lease.run, llm_request_count=2),
        )
        stream_started = asyncio.Event()

        class Repository:
            def __init__(self) -> None:
                self.status = RunStatus.RUNNING
                self.settled_counts: list[int | None] = []

            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def is_live_execution(self, *_args: object) -> bool:
                return self.status is RunStatus.RUNNING

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *_args: object) -> tuple[object, ...]:
                return (_user_trigger_message(lease.run),)

            async def list_successfully_loaded_skill_ids(self, *_args: object) -> tuple[str, ...]:
                return ()

            async def record_usage(self, *_args: object, **_kwargs: object) -> None:
                # 模拟生产仓储在 Run 已进入 CANCELLING 时拒绝写入 usage 并返回 None。
                return None

            async def heartbeat(self, *_args: object, **_kwargs: object) -> Run:
                return replace(lease.run, status=self.status)

            async def get_lease_state(self, *_args: object) -> RunLeaseState:
                return RunLeaseState(
                    status=self.status,
                    worker_id=lease.run.worker_id,
                    execution_epoch=lease.run.execution_epoch,
                )

            async def settle_cancelled(
                self,
                run_id: str,
                worker_id: str,
                execution_epoch: int,
                settled_at: datetime,
                *,
                llm_request_count: int | None = None,
            ) -> Run:
                del run_id, worker_id, execution_epoch, settled_at
                self.settled_counts.append(llm_request_count)
                self.status = RunStatus.CANCELLED
                return replace(
                    lease.run,
                    status=RunStatus.CANCELLED,
                    llm_request_count=llm_request_count or lease.run.llm_request_count,
                )

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                return _runtime_profile(profile_id)

        class BlockingProvider:
            @asynccontextmanager
            async def stream(
                self, _runtime: RuntimeModelProfile, _request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    stream_started.set()
                    await asyncio.Event().wait()
                    if False:
                        yield TextDelta(0, 0, "")

                yield _events()

        repository = Repository()
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, Profiles()),
            cast(LlmStreamProvider, BlockingProvider()),
        )
        worker = RunWorker(
            cast(RunRepository, repository),
            executor,
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.01,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
        )

        drive_task = asyncio.create_task(worker._drive_lease(lease))
        await asyncio.wait_for(stream_started.wait(), timeout=2.0)
        # 在途 LLM 已预占 1 次预算（2 -> 3），此时外部发起取消。
        repository.status = RunStatus.CANCELLING
        await asyncio.wait_for(drive_task, timeout=2.0)

        assert repository.settled_counts == [3]
        assert executor.pop_llm_request_count(lease.run.id, lease.run.execution_epoch) is None

    asyncio.run(scenario())


def test_worker_timeout_forwards_llm_request_count_to_fail_command() -> None:
    """Worker 侧执行超时中止在途 LLM 时，FailRunCommand 须携带已预占的 llm_request_count。"""

    async def scenario() -> None:
        base_now = datetime.now(UTC)
        deadline = base_now + timedelta(seconds=5)
        base_lease = _lease()
        lease = replace(
            base_lease,
            run=replace(
                base_lease.run,
                llm_request_count=1,
                execution_deadline_at=deadline,
            ),
            execution_deadline_at=deadline,
        )
        stream_started = asyncio.Event()

        class Repository:
            def __init__(self) -> None:
                self.fail_commands: list[FailRunCommand] = []

            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *_args: object) -> tuple[object, ...]:
                return (_user_trigger_message(lease.run),)

            async def list_successfully_loaded_skill_ids(self, *_args: object) -> tuple[str, ...]:
                return ()

            async def record_usage(self, *_args: object, **_kwargs: object) -> None:
                # 模拟超过 execution_deadline_at 后 record_usage 返回 None。
                return None

            async def heartbeat(self, *_args: object, **_kwargs: object) -> Run:
                return lease.run

            async def get_lease_state(self, *_args: object) -> RunLeaseState:
                return RunLeaseState(
                    status=RunStatus.RUNNING,
                    worker_id=lease.run.worker_id,
                    execution_epoch=lease.run.execution_epoch,
                )

            async def get(self, *_args: object) -> Run:
                return replace(lease.run, status=RunStatus.FAILED)

            async def fail(self, command: FailRunCommand) -> Run:
                self.fail_commands.append(command)
                return replace(
                    lease.run,
                    status=RunStatus.FAILED,
                    safe_error_code=command.safe_error_code,
                    llm_request_count=command.llm_request_count or lease.run.llm_request_count,
                )

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                return _runtime_profile(profile_id)

        class BlockingProvider:
            @asynccontextmanager
            async def stream(
                self, _runtime: RuntimeModelProfile, _request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    stream_started.set()
                    await asyncio.Event().wait()
                    if False:
                        yield TextDelta(0, 0, "")

                yield _events()

        repository = Repository()
        # Executor 看到的剩余时间充裕，Worker 侧仅余 80ms，确保稳定命中 Worker 超时分支。
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, Profiles()),
            cast(LlmStreamProvider, BlockingProvider()),
            clock=lambda: base_now,
        )
        worker = RunWorker(
            cast(RunRepository, repository),
            executor,
            RunWorkerConfig(
                concurrency=1,
                lease_seconds=3,
                heartbeat_seconds=1,
                execution_seconds=30,
                cancel_poll_seconds=0.01,
                queue_poll_seconds=0.05,
                reclaim_seconds=5,
            ),
            clock=lambda: base_now + timedelta(seconds=4.92),
        )

        drive_task = asyncio.create_task(worker._drive_lease(lease))
        await asyncio.wait_for(stream_started.wait(), timeout=2.0)
        await asyncio.wait_for(drive_task, timeout=2.0)

        assert len(repository.fail_commands) == 1
        assert repository.fail_commands[0].safe_error_code == "execution_timeout"
        assert repository.fail_commands[0].llm_request_count == 2
        assert executor.pop_llm_request_count(lease.run.id, lease.run.execution_epoch) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("current_status", "expected_count"),
    [
        (RunStatus.RUNNING, 4),
        (RunStatus.CANCELLING, 5),
    ],
)
def test_worker_finalize_execution_forwards_llm_request_count(
    current_status: RunStatus,
    expected_count: int,
) -> None:
    """_finalize_execution 兜底失败与取消竞态均须透传并回收执行器中的在途请求计数。"""
    from app.run.budget import RunLlmBudget

    async def scenario() -> None:
        lease = _lease()
        fail_commands: list[FailRunCommand] = []
        settled_counts: list[int | None] = []

        class Repository:
            async def get(self, *_args: object, **_kwargs: object) -> Run:
                return replace(lease.run, status=current_status, event_sequence=2)

            async def fail(self, command: FailRunCommand) -> Run:
                fail_commands.append(command)
                return replace(lease.run, status=RunStatus.FAILED, event_sequence=3)

            async def settle_cancelled(
                self,
                run_id: str,
                worker_id: str,
                execution_epoch: int,
                settled_at: datetime,
                *,
                llm_request_count: int | None = None,
            ) -> Run:
                del run_id, worker_id, execution_epoch, settled_at
                settled_counts.append(llm_request_count)
                return replace(lease.run, status=RunStatus.CANCELLED, event_sequence=3)

        executor = RunExecutor(
            cast(RunRepository, Repository()),
            cast(ModelProfileRepository, object()),
            cast(LlmStreamProvider, object()),
        )
        executor._register_active_budget(
            lease.run,
            RunLlmBudget(used=expected_count, maximum=40),
        )
        worker = RunWorker(
            cast(RunRepository, Repository()),
            executor,
            RunWorkerConfig(),
        )

        async def finished_without_terminal() -> None:
            return None

        task = asyncio.create_task(finished_without_terminal())
        await task
        await worker._finalize_execution(lease, task)

        if current_status is RunStatus.RUNNING:
            assert len(fail_commands) == 1
            assert fail_commands[0].safe_error_code == "execution_failed"
            assert fail_commands[0].llm_request_count == expected_count
            assert settled_counts == []
        else:
            assert settled_counts == [expected_count]
            assert fail_commands == []
        # 收敛后应已弹出，避免残留引用。
        assert executor.pop_llm_request_count(lease.run.id, lease.run.execution_epoch) is None

    asyncio.run(scenario())
