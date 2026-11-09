"""Run Worker 生命周期边界。"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from run_worker import run_worker_process

from app.run.config import RunWorkerConfig
from app.run.context import _context_threads, _run_context_work, prepare_run_tokenizers
from app.run.executor import RunExecutor
from app.run.worker import RunWorker
from domain.llm.model import LlmRequest, LlmStreamEvent, TextDelta
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.run.model import FailRunCommand, Run, RunLease, RunStatus, RunUsage
from domain.run.ports import RunRepository


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
        prompt_template_version="run-v1",
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

    async def settle_cancelled(self, *args: object, **kwargs: object) -> Run:
        self.settled = True
        self.settled_event.set()
        return self.lease.run

    async def reclaim_expired(self, **kwargs: object) -> tuple[Run, ...]:
        return ()


class _SlowCleanupExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    async def execute(self, lease: RunLease) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.cleanup_release.wait()
            raise


class _FinishedExecutor:
    async def execute(self, lease: RunLease) -> None:
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
            def __init__(self) -> None:
                self.llm_count = 0

            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def reserve_llm_request(self, command: object) -> int:
                self.llm_count += 1
                return self.llm_count

            async def list_retry_context(self, *_args: object) -> tuple[object, ...]:
                return ()

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *args: object) -> tuple[object, ...]:
                return ()

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def record_usage(self, command: object) -> None:
                usage_attempted.set()
                return None

            async def fail(self, command: object) -> None:
                return None

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        credential_env="ATHENA_LLM_CREDENTIAL_TEST",
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
                        created_at=datetime.now(UTC),
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
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
            def __init__(self) -> None:
                self.llm_count = 0

            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def reserve_llm_request(self, command: object) -> int:
                self.llm_count += 1
                return self.llm_count

            async def list_retry_context(self, *_args: object) -> tuple[object, ...]:
                return ()

            async def get_resumable_tool_batch(self, *_args: object) -> None:
                return None

            async def list_context_messages(self, *args: object) -> tuple[object, ...]:
                return ()

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
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        credential_env="ATHENA_LLM_CREDENTIAL_TEST",
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
                        created_at=datetime.now(UTC),
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
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


def test_worker_renews_lease_until_slow_cancel_cleanup_finishes() -> None:
    """取消流清理期间持续续租，直到 Executor 关闭后才落 cancelled。"""

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
            await asyncio.wait_for(repository.fourth_heartbeat.wait(), timeout=4.5)
            assert not repository.settled
        finally:
            executor.cleanup_release.set()
            await worker.stop()
            await asyncio.wait_for(worker_task, timeout=2)
        assert repository.heartbeats >= 2
        assert repository.settled

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


def test_worker_entry_propagates_fatal_task_error_and_closes_resources() -> None:
    """Worker 启动后立即失败时入口报错，并释放 Provider 与数据库。"""

    async def scenario() -> None:
        state = SimpleNamespace(provider_closed=False, database_closed=False, stopped=False)

        class FakeDatabase:
            session_factory = object()

            def __init__(self, database_url: str) -> None:
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
            patch("run_worker.McpSecretCipher.from_environment", return_value=object()),
            patch("run_worker.Database", FakeDatabase),
            patch("run_worker.SqlAlchemyModelProfileRepository", lambda _: object()),
            patch("run_worker.SqlAlchemyMessageRepository", lambda _: object()),
            patch("run_worker.SqlAlchemyRunRepository", lambda *_: object()),
            patch("run_worker.OpenAIChatCompletionsAdapter", FakeProvider),
            patch("run_worker.RunExecutor", lambda *_args, **_kwargs: object()),
            patch("run_worker.RunWorker", FakeWorker),
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


def test_expired_deadline_after_thread_start_keeps_slot_until_thread_exit() -> None:
    """start 已成功但 deadline 已过时，槽位仍由运行线程独占至退出。"""

    async def scenario() -> None:
        gate = threading.Event()
        thread_finished = threading.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        original_start = threading.Thread.start

        def blocked_work() -> None:
            try:
                gate.wait()
                raise ValueError("late_tokenizer_failure")
            finally:
                thread_finished.set()

        def start_then_delay(thread: threading.Thread, *args: Any, **kwargs: Any) -> None:
            original_start(thread, *args, **kwargs)
            if thread.name == "run-tokenizer":
                time.sleep(0.05)

        with patch("app.run.context.threading.Thread.start", start_then_delay):
            with pytest.raises(TimeoutError, match="run_context_work_timeout"):
                await _run_context_work(blocked_work, timeout_seconds=0.01)

        acquired = [_context_threads.acquire(blocking=False) for _ in range(4)]
        assert sum(acquired) == 3
        for slot in acquired:
            if slot:
                _context_threads.release()

        gate.set()
        deadline = asyncio.get_running_loop().time() + 1
        released = [False, False, False, False]
        while asyncio.get_running_loop().time() < deadline:
            released = [_context_threads.acquire(blocking=False) for _ in range(4)]
            if all(released):
                break
            for slot in released:
                if slot:
                    _context_threads.release()
            await asyncio.sleep(0.005)
        assert thread_finished.is_set()
        assert all(released)
        for _ in released:
            _context_threads.release()
        await asyncio.sleep(0)
        assert loop_errors == []

    asyncio.run(scenario())


def test_executor_does_not_start_provider_after_context_work_deadline() -> None:
    """上下文预算工作超时后 Run 失败且不发起模型调用。"""

    async def scenario() -> None:
        failed = asyncio.Event()
        provider_called = False

        class Repository:
            def __init__(self) -> None:
                self.llm_count = 0

            async def initialize_tools(self, command: Any) -> Any:
                return command.snapshot

            async def reserve_llm_request(self, command: object) -> int:
                self.llm_count += 1
                return self.llm_count

            async def list_retry_context(self, *_args: object) -> tuple[object, ...]:
                return ()

            async def list_context_messages(self, *_args: object) -> tuple[object, ...]:
                return ()

            async def is_live_execution(self, *_args: object) -> bool:
                return True

            async def fail(self, *_args: object, **_kwargs: object) -> None:
                failed.set()

        class Profiles:
            async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=profile_id,
                        name="test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test",
                        credential_env="ATHENA_LLM_CREDENTIAL_TEST",
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
                        created_at=datetime.now(UTC),
                    ),
                    api_key="test-secret",
                )

        class Provider:
            @asynccontextmanager
            async def stream(
                self, _runtime: RuntimeModelProfile, _request: LlmRequest
            ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
                nonlocal provider_called
                provider_called = True
                yield _empty_events()

        async def slow_context_count(*_args: object, **_kwargs: object) -> object:
            await asyncio.sleep(1)
            return (), (), 1

        with patch("app.run.executor.build_llm_request", slow_context_count):
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
