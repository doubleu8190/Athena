"""Run SSE 服务、权威快照和 Cookie 认证回归。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from starlette.types import Message, Send
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
)

from adapter.run import redis_transport as redis_module
from adapter.run.redis_transport import RedisRunTransport
from api.runs import _RunEventSourceResponse, build_run_router
from app.run.service import RunService
from app.run.stream import RunStreamService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.auth.ports import Authenticator
from domain.run.model import (
    ContextStopReason,
    Run,
    RunCompressionCompletedPayload,
    RunEvent,
    RunFailedPayload,
    RunInterruptedEvent,
    RunModelSnapshot,
    RunPersistedEvent,
    RunPersistedEventPayload,
    RunQueuedPayload,
    RunSnapshotEvent,
    RunStatus,
    RunStreamEvent,
    RunStreamMessage,
    RunStreamSnapshot,
    RunTextDeltaEvent,
    RunTextDeltaPayload,
    RunUsage,
)
from domain.run.ports import (
    RunEventSubscription,
    RunEventTransport,
    RunEventTransportError,
    RunRepository,
)


def _run(status: RunStatus, epoch: int) -> Run:
    now = datetime.now(UTC)
    return Run(
        id="run_stream_test",
        session_id="session_stream_test",
        owner_id="user_stream_test",
        status=status,
        trigger_message_id="message_stream_test",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile_stream_test",
        model_snapshot=RunModelSnapshot(
            id="profile_stream_test",
            name="Stream Test Model",
            protocol="chat_completions",
            base_url="https://api.example.com/v1",
            model="gpt-4o-mini",
            context_window_tokens=128000,
            tokenizer_encoding="cl100k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=4096,
            request_timeout_seconds=30,
            execution_timeout_seconds=120,
            max_concurrency=4,
        ),
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=epoch,
        worker_id="worker-stream-test" if status is RunStatus.RUNNING else None,
        lease_expires_at=now + timedelta(seconds=30) if status is RunStatus.RUNNING else None,
        execution_started_at=now if status is RunStatus.RUNNING else None,
        execution_deadline_at=now + timedelta(seconds=60) if status is RunStatus.RUNNING else None,
        queued_at=now,
        first_started_at=now,
        finished_at=now if status is RunStatus.COMPLETED else None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )


@dataclass
class _FakeRepository:
    status: RunStatus = RunStatus.COMPLETED
    epoch: int = 1
    subscribed: bool = False
    valid_epochs: set[int] | None = None
    auth_calls: int = 0

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        return _run(self.status, self.epoch)

    async def get_stream_snapshot(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunStreamSnapshot | None:
        assert self.subscribed
        return RunStreamSnapshot(_run(self.status, self.epoch), (), ())

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        return execution_epoch in (self.valid_epochs or set())

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
        return ()


class _FakeSubscription:
    def __init__(self, events: list[RunStreamEvent]) -> None:
        self.events = deque(events)

    async def get(self, *, timeout: float) -> RunStreamEvent | None:
        if self.events:
            return self.events.popleft()
        await asyncio.sleep(min(timeout, 0.01))
        return None

    async def aclose(self) -> None:
        return None


class _FakeTransport:
    def __init__(
        self,
        repository: _FakeRepository,
        events: list[RunStreamEvent] | None = None,
    ) -> None:
        self.repository = repository
        self.events = events or []

    async def publish_text(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int = 0,
    ) -> None:
        return None

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: object) -> None:
        return None

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
        self.repository.subscribed = True
        yield cast(RunEventSubscription, _FakeSubscription(self.events))


class _FakeAuthenticator:
    def __init__(self, *, revoked: bool = False) -> None:
        self.revoked = revoked
        self.calls = 0

    async def authenticate(self, token: str) -> AuthContext | None:
        self.calls += 1
        if self.revoked:
            return None
        now = datetime.now(UTC)
        user = User(
            id="user_stream_test",
            email="stream@example.test",
            password_hash="unused",
            display_name="stream user",
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            role=UserRole.MEMBER,
        )
        return AuthContext(user=user, session_id="auth-session")


def test_stream_subscribes_before_snapshot_and_reconciles_epoch_from_database() -> None:
    repository = _FakeRepository(
        status=RunStatus.RUNNING,
        epoch=0,
        valid_epochs={2},
    )
    transport = _FakeTransport(
        repository,
        [
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=1,
                stream_sequence=1,
                payload=RunTextDeltaPayload(text="stale"),
            ),
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=2,
                stream_sequence=2,
                payload=RunTextDeltaPayload(text="current"),
            ),
        ],
    )
    auth = _FakeAuthenticator()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, auth),
        cast(RunEventTransport, transport),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        events = [await anext(iterator), await anext(iterator)]
        await iterator.aclose()
        return events

    events = asyncio.run(collect())
    assert isinstance(events[0], RunSnapshotEvent)
    assert isinstance(events[1], RunTextDeltaEvent)
    assert events[1].execution_epoch == 2
    assert events[1].payload.text == "current"
    assert repository.subscribed


def test_failed_reconnect_snapshot_carries_persisted_report_reference() -> None:
    """断线重连进入失败终态时，快照和 failed 事件都指向持久化报告。"""
    now = datetime.now(UTC)
    report_id = "message_context_safety_report"

    class FailedRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.FAILED, epoch=1)

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            failed_run = replace(
                _run(RunStatus.FAILED, 1),
                event_sequence=2,
                safe_error_code="run_context_safety_stop",
                report_message_id=report_id,
            )
            report_message = RunStreamMessage(
                id=report_id,
                role="assistant",
                status="completed",
                text="上下文安全停止。任务未标记为完成。",
                sequence=2,
                created_at=now,
                session_id="session_stream_test",
                parts=({"type": "text", "text": "上下文安全停止。任务未标记为完成。"},),
            )
            failed_event = RunEvent(
                id="event_context_safety_failed",
                run_id="run_stream_test",
                sequence=2,
                type="failed",
                created_at=now,
                payload=RunFailedPayload(
                    safe_error_code="run_context_safety_stop",
                    report_message_id=report_id,
                    context_stop_reason=ContextStopReason.COMPRESSION_INSUFFICIENT,
                ),
            )
            return RunStreamSnapshot(
                failed_run,
                (report_message,),
                (failed_event,),
            )

    repository = FailedRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> RunSnapshotEvent:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        first = await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await iterator.aclose()
        assert isinstance(first, RunSnapshotEvent)
        return first

    snapshot = asyncio.run(collect())
    assert snapshot.payload.report_message_id == report_id
    assert snapshot.payload.messages[0].id == report_id
    failed_event = snapshot.payload.events[0]
    assert isinstance(failed_event.payload, RunFailedPayload)
    assert failed_event.payload.report_message_id == report_id
    assert failed_event.payload.context_stop_reason is ContextStopReason.COMPRESSION_INSUFFICIENT


def test_stream_stops_after_snapshot_while_waiting_for_tool_approval() -> None:
    repository = _FakeRepository(status=RunStatus.WAITING_FOR_APPROVAL, epoch=2)
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> RunSnapshotEvent:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        first = await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await iterator.aclose()
        assert isinstance(first, RunSnapshotEvent)
        return first

    first = asyncio.run(collect())
    assert isinstance(first, RunSnapshotEvent)
    assert first.payload.status is RunStatus.WAITING_FOR_APPROVAL
    assert repository.subscribed


def test_stream_assigns_monotonic_sequence_and_advances_live_epoch() -> None:
    """SSE 编排层统一分配单调递增的 stream_sequence，并在高代次存活时切换代次转发。"""
    repository = _FakeRepository(
        status=RunStatus.RUNNING,
        epoch=1,
        valid_epochs={1, 2},
    )
    transport = _FakeTransport(
        repository,
        [
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=1,
                stream_sequence=0,
                payload=RunTextDeltaPayload(text="first"),
            ),
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=1,
                stream_sequence=0,
                payload=RunTextDeltaPayload(text="second"),
            ),
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=2,
                stream_sequence=0,
                payload=RunTextDeltaPayload(text="new execution epoch"),
            ),
        ],
    )
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, transport),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        events = [await anext(iterator) for _ in range(4)]
        await iterator.aclose()
        return events

    events = asyncio.run(collect())
    assert [event.stream_sequence for event in events] == [0, 1, 2, 3]
    assert isinstance(events[1], RunTextDeltaEvent)
    assert events[1].payload.text == "first"
    assert isinstance(events[2], RunTextDeltaEvent)
    assert events[2].payload.text == "second"
    assert isinstance(events[3], RunTextDeltaEvent)
    assert events[3].execution_epoch == 2
    assert events[3].payload.text == "new execution epoch"


def test_stream_pages_all_events_beyond_initial_snapshot() -> None:
    """首屏仅携带100条事件时，流仍补齐同一快照游标内的余项。"""
    now = datetime.now(UTC)

    def event(sequence: int) -> RunEvent:
        return RunEvent(
            id=f"event_{sequence}",
            run_id="run_stream_test",
            sequence=sequence,
            type="queued",
            created_at=now,
            payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
        )

    class ManyEventsRepository(_FakeRepository):
        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            return RunStreamSnapshot(
                replace(_run(RunStatus.RUNNING, 1), event_sequence=105),
                (),
                tuple(event(sequence) for sequence in range(1, 101)),
                events_complete=False,
                events_after_sequence=100,
            )

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
            upper = min(through_sequence or 105, 105)
            return tuple(event(sequence) for sequence in range(after_sequence + 1, upper + 1))[
                :limit
            ]

    repository = ManyEventsRepository(status=RunStatus.RUNNING, epoch=1)
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        events = [await anext(iterator) for _ in range(6)]
        await iterator.aclose()
        return events

    events = asyncio.run(collect())
    assert isinstance(events[0], RunSnapshotEvent)
    assert [event.payload.event.sequence for event in events[1:] if event.type == "run_event"] == [
        101,
        102,
        103,
        104,
        105,
    ]


def test_run_sse_asgi_uses_cookie_auth_and_reports_reconnect_snapshot() -> None:
    repository = _FakeRepository(status=RunStatus.COMPLETED)
    auth = _FakeAuthenticator()
    stream_service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, auth),
        cast(RunEventTransport, _FakeTransport(repository)),
    )
    app = FastAPI()
    app.state.authenticator = auth
    app.state.auth_cookie_name = "custom_session_cookie"
    app.include_router(build_run_router(cast(RunService, object()), stream_service))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/sessions/session_stream_test/runs/run_stream_test/stream",
            cookies={"custom_session_cookie": "valid-token"},
            headers={"Last-Event-ID": "123"},
        )
    assert response.status_code == 200
    assert "event: stream_interrupted" in response.text
    assert "event: snapshot" in response.text
    assert repository.subscribed
    assert auth.calls == 1


def test_run_stream_replays_compression_payload_without_summary_text() -> None:
    """SSE 重放压缩状态事件及安全字段，不包含摘要正文。"""
    now = datetime.now(UTC)
    event = RunEvent(
        id="event_compression_test",
        run_id="run_stream_test",
        sequence=1,
        type="compression_completed",
        created_at=now,
        payload=RunCompressionCompletedPayload(
            operation_id="cmpop_stream_test",
            summary_id="csum_stream_test",
            covered_through_sequence=5,
            batch_count=2,
        ),
    )

    class _CompressionEventRepository(_FakeRepository):
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
            return (event,) if after_sequence < event.sequence else ()

    repository = _CompressionEventRepository(status=RunStatus.COMPLETED, epoch=1)
    stream = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            item
            async for item in stream.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    events = asyncio.run(collect())
    persisted = next(item for item in events if item.type == "run_event")
    payload = persisted.payload.event.payload
    assert isinstance(payload, RunCompressionCompletedPayload)
    assert payload.operation_id == "cmpop_stream_test"
    serialized = persisted.model_dump_json()
    assert "csum_stream_test" in serialized
    assert "summary body" not in serialized


def test_public_sse_route_enforces_full_utf8_frame_budget_with_long_ids() -> None:
    """公开 SSE 路由按完整帧字节数验证中文、长 ID 和长序号。"""
    run_id = "r" * 64
    session_id = "s" * 64
    text_value = "中" * 21_000
    auth = _FakeAuthenticator()

    class LongFrameService:
        async def authorize(self, session: str, owner: str, run: str) -> bool:
            return session == session_id and run == run_id and owner == "user_stream_test"

        async def events(
            self,
            session: str,
            owner: str,
            run: str,
            token: str,
            *,
            last_event_id: str | None = None,
        ) -> AsyncIterator[RunStreamEvent]:
            yield RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=9,
                stream_sequence=9_999_999_999_999_999_999,
                payload=RunTextDeltaPayload(text=text_value),
            )

    app = FastAPI()
    app.state.authenticator = auth
    app.include_router(
        build_run_router(cast(RunService, object()), cast(RunStreamService, LongFrameService()))
    )
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
            cookies={"athena_session": "valid-token"},
        )
    assert response.status_code == 200
    frames = [frame for frame in response.text.split("\n\n") if frame]
    assert len(frames) == 1
    assert len((frames[0] + "\n\n").encode("utf-8")) <= 64 * 1024
    assert text_value in response.text


def test_public_sse_preserves_model_round_for_each_delta() -> None:
    """SSE 不覆盖同一次执行内多个模型轮次的文本增量。"""
    run_id = "run_round_stream"
    session_id = "session_round_stream"

    class MultiRoundService:
        async def authorize(self, session: str, owner: str, run: str) -> bool:
            return session == session_id and run == run_id and owner == "user_stream_test"

        async def events(
            self,
            session: str,
            owner: str,
            run: str,
            token: str,
            *,
            last_event_id: str | None = None,
        ) -> AsyncIterator[RunStreamEvent]:
            del session, owner, token, last_event_id
            for round_number in (1, 2, 3):
                yield RunTextDeltaEvent(
                    run_id=run,
                    execution_epoch=4,
                    stream_sequence=round_number,
                    payload=RunTextDeltaPayload(
                        text=f"round-{round_number}",
                        round=round_number,
                    ),
                )

    app = FastAPI()
    app.state.authenticator = _FakeAuthenticator()
    app.include_router(
        build_run_router(
            cast(RunService, object()),
            cast(RunStreamService, MultiRoundService()),
        )
    )
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
            cookies={"athena_session": "valid-token"},
        )
    assert response.status_code == 200
    for round_number in (1, 2, 3):
        assert f"round-{round_number}" in response.text
        assert f'"round":{round_number}' in response.text


def test_database_reconciliation_delivers_result_after_lost_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pub/Sub 丢失正式通知时，周期快照仍送达最终消息。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 0.0)
    repository = _FakeRepository(status=RunStatus.RUNNING, epoch=1)
    now = datetime.now(UTC)
    final_message = RunStreamMessage(
        id="assistant_final",
        role="assistant",
        status="completed",
        text="database committed result",
        sequence=2,
        created_at=now,
        session_id="session_stream_test",
        parts=({"type": "text", "text": "database committed result"},),
    )

    class CommittedRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshots = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            self.snapshots += 1
            if self.snapshots == 1:
                return RunStreamSnapshot(_run(RunStatus.RUNNING, 1), (), ())
            return RunStreamSnapshot(_run(RunStatus.COMPLETED, 1), (final_message,), ())

    repository = CommittedRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        events = [await anext(iterator), await anext(iterator)]
        await iterator.aclose()
        return events

    events = asyncio.run(collect())
    assert isinstance(events[1], RunSnapshotEvent)
    assert events[1].payload.messages[0].text == "database committed result"


def test_stream_stops_when_cookie_authentication_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长连接周期认证失败后结束流，不继续使用旧 AuthContext。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "AUTH_RECHECK_SECONDS", 0.0)
    repository = _FakeRepository(status=RunStatus.RUNNING, epoch=1)
    auth = _FakeAuthenticator(revoked=True)
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, auth),
        cast(RunEventTransport, _FakeTransport(repository)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        result = [await anext(iterator)]
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await iterator.aclose()
        return result

    assert isinstance(asyncio.run(collect())[0], RunSnapshotEvent)
    assert auth.calls == 1


def test_pubsub_disconnect_emits_redis_unavailable_and_reconciles_from_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pub/Sub 中断后发出 redis_unavailable 并降级依赖周期 DB 对账恢复终态快照。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 0.01)

    class ChangingRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshots = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            self.subscribed = True
            self.snapshots += 1
            status = RunStatus.RUNNING if self.snapshots == 1 else RunStatus.COMPLETED
            return RunStreamSnapshot(_run(status, 1), (), ())

    class BrokenSubscription(_FakeSubscription):
        async def get(self, *, timeout: float) -> RunStreamEvent | None:
            raise RunEventTransportError("subscriber disconnected")

    class BrokenTransport(_FakeTransport):
        @asynccontextmanager
        async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
            del run_id
            self.repository.subscribed = True
            yield cast(RunEventSubscription, BrokenSubscription([]))

    repository = ChangingRepository()
    transport = BrokenTransport(repository)
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, transport),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            event
            async for event in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    events = asyncio.run(collect())
    assert isinstance(events[0], RunSnapshotEvent)
    assert events[1].type == "stream_interrupted"
    assert events[1].payload.reason == "redis_unavailable"
    assert isinstance(events[2], RunSnapshotEvent)
    assert events[2].payload.status is RunStatus.COMPLETED


def test_publisher_failure_translates_error_without_blocking_subsequent_publishes() -> None:
    """单次发布遇到 RedisError 抛出 RunEventTransportError，后续恢复发布仍正常写出。"""
    from redis.exceptions import RedisError

    class FailingRedis:
        def __init__(self) -> None:
            self.fail_next_publish = True
            self.published: list[bytes] = []

        async def publish(self, channel: str, raw: bytes) -> int:
            del channel
            if self.fail_next_publish:
                self.fail_next_publish = False
                raise RedisError("publish unavailable")
            self.published.append(raw)
            return 1

    async def scenario() -> list[RunStreamEvent]:
        client = FailingRedis()
        transport = RedisRunTransport(cast(Redis, client))
        with pytest.raises(RunEventTransportError, match="run_stream_publish_failed"):
            await transport.publish_text("run_failure_test", 3, "lost delta")
        await transport.publish_text("run_failure_test", 3, "next delta")
        return [redis_module._EVENT_ADAPTER.validate_json(raw) for raw in client.published]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], RunTextDeltaEvent)
    assert events[0].stream_sequence == 0
    assert events[0].payload.text == "next delta"


def test_sse_disconnects_when_asgi_send_exceeds_timeout() -> None:
    """慢客户端的 ASGI 写入超过期限时关闭响应。"""

    async def scenario() -> None:
        async def body() -> AsyncIterator[str]:
            yield "event: heartbeat\ndata: {}\n\n"

        response = _RunEventSourceResponse(body(), media_type="text/event-stream")

        async def slow_send(message: Message) -> None:
            await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await response.stream_response(cast(Send, slow_send))

    asyncio.run(scenario())


def test_redis_subscription_get_translates_client_errors_to_transport_error() -> None:
    """独立订阅读取阶段的 Redis 客户端异常翻译为领域传输异常并保留原因。"""
    from redis.exceptions import RedisError

    class FailingPubSub:
        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            raise RedisError("pubsub read failed")

        async def aclose(self) -> None:
            return None

    class FailingRedis:
        def pubsub(self) -> FailingPubSub:
            return FailingPubSub()

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = RedisRunTransport(cast(Redis, FailingRedis()))
        async with transport.subscribe("run_read_fail") as subscription:
            with pytest.raises(
                RunEventTransportError, match="run_stream_transport_failed"
            ) as caught:
                await subscription.get(timeout=0.1)
            assert isinstance(caught.value.__cause__, RedisError)
        await transport.aclose()

    asyncio.run(scenario())


def test_redis_subscribe_translates_client_errors_to_transport_error() -> None:
    """订阅握手阶段的 Redis 客户端异常翻译为领域传输异常并保留原因。"""
    from redis.exceptions import RedisError

    class FailingPubSub:
        async def subscribe(self, channel_name: str) -> None:
            del channel_name
            raise RedisError("subscribe failed")

        async def aclose(self) -> None:
            return None

    class FailingRedis:
        def pubsub(self) -> FailingPubSub:
            return FailingPubSub()

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = RedisRunTransport(cast(Redis, FailingRedis()))
        with pytest.raises(RunEventTransportError, match="run_stream_subscribe_failed") as caught:
            async with transport.subscribe("run_x"):
                pass
        assert isinstance(caught.value.__cause__, RedisError)
        await transport.aclose()

    asyncio.run(scenario())


def test_stream_validates_deltas_by_authoritative_epoch_without_per_delta_db_polling() -> None:
    """权威 epoch 内的连续增量与中断零 DB 轮询；旧代次直接拦截，新代次跃升仅复核 1 次。"""
    from domain.run.model import RunInterruptedPayload

    class CountingRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=2, valid_epochs={3})
            self.live_checks: list[int] = []

        async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
            del run_id
            self.live_checks.append(execution_epoch)
            return execution_epoch in (self.valid_epochs or set())

    incoming: list[RunStreamEvent] = [
        # 旧代次僵尸 delta：直接丢弃，零 DB 查询。
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="stale-epoch-1"),
        ),
        # 当前权威 epoch=2 连续增量与中断：全部零 DB 查询。
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="a"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="b"),
        ),
        RunInterruptedEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=3,
            payload=RunInterruptedPayload(reason="publisher_error"),
        ),
        # 新代次跃升至 epoch=4（DB 判定已失效）：复核 1 次并拦截，不推进当前 epoch。
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=4,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="stale-future-epoch-4"),
        ),
        # 新代次跃升至 epoch=3（DB 判定存活）：复核 1 次并推进 epoch，后续同代次增量零查库。
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=3,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="c"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=3,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="d"),
        ),
    ]
    repository = CountingRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        # 1 snapshot + 2 deltas(epoch=2) + 1 interrupted(epoch=2) + 2 deltas(epoch=3) = 6
        items = [await anext(iterator) for _ in range(6)]
        await iterator.aclose()
        return items

    events = asyncio.run(collect())
    assert [event.payload.text for event in events if isinstance(event, RunTextDeltaEvent)] == [
        "a",
        "b",
        "c",
        "d",
    ]
    # epoch=1/2 零查库；仅在观察到更高代次 4、3 时各向 DB 复核 1 次。
    assert repository.live_checks == [4, 3]


def test_stream_suppresses_deltas_after_reconcile_observes_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周期对账观察到 cancelling 状态后拦截同代次僵尸 delta。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 0.0)

    class CancellingRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshots = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            self.snapshots += 1
            if self.snapshots == 1:
                return RunStreamSnapshot(_run(RunStatus.RUNNING, 1), (), ())
            if self.snapshots == 2:
                return RunStreamSnapshot(_run(RunStatus.CANCELLING, 1), (), ())
            return RunStreamSnapshot(_run(RunStatus.CANCELLED, 1), (), ())

    incoming: list[RunStreamEvent] = [
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="before-cancel"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="zombie-after-cancel"),
        ),
    ]
    repository = CancellingRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            item
            async for item in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    events = asyncio.run(collect())
    texts = [event.payload.text for event in events if isinstance(event, RunTextDeltaEvent)]
    assert texts == ["before-cancel"]
    assert isinstance(events[-1], RunSnapshotEvent)
    assert events[-1].payload.status is RunStatus.CANCELLED


def test_stream_deduplicates_batch_persisted_events_already_covered_by_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同批 5 条持久化事件在首次对账覆盖后不再重复触发全量快照，同时保留 gap 检测。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 60.0)
    now = datetime.now(UTC)

    def persisted_run_event(sequence: int) -> RunEvent:
        return RunEvent(
            id=f"evt_{sequence}",
            run_id="run_stream_test",
            sequence=sequence,
            type="queued",
            created_at=now,
            payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
        )

    class BatchSnapshotRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshot_calls = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                # 首屏快照只包含 sequence=1。
                return RunStreamSnapshot(
                    replace(_run(RunStatus.RUNNING, 1), event_sequence=1),
                    (),
                    (persisted_run_event(1),),
                )
            # 收到同批第 1 条通知触发对账时，数据库已提交 sequence 2..6 共 5 条事件。
            return RunStreamSnapshot(
                replace(_run(RunStatus.RUNNING, 1), event_sequence=6),
                (),
                tuple(persisted_run_event(seq) for seq in range(1, 7)),
            )

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
            upper = min(through_sequence or 6, 6)
            return tuple(persisted_run_event(seq) for seq in range(after_sequence + 1, upper + 1))[
                :limit
            ]

    # 模拟同一批 5 条 RunPersistedEvent（seq 2..6）按 Pub/Sub 依次到达，
    # 并在第 5 条故意跳号 stream_sequence 验证 transport_cursor 与 stream_gap 检测未被绕过。
    batch_incoming: list[RunStreamEvent] = [
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunPersistedEventPayload(event=persisted_run_event(2)),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=2,
            payload=RunPersistedEventPayload(event=persisted_run_event(3)),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=3,
            payload=RunPersistedEventPayload(event=persisted_run_event(4)),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=4,
            payload=RunPersistedEventPayload(event=persisted_run_event(5)),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=6,
            payload=RunPersistedEventPayload(event=persisted_run_event(6)),
        ),
    ]
    repository = BatchSnapshotRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, batch_incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        # 1 初始 snapshot + 5 条补齐的 persisted events (seq 2..6) = 6
        emitted = [await anext(iterator) for _ in range(6)]
        await iterator.aclose()
        return emitted

    emitted = asyncio.run(collect())
    assert isinstance(emitted[0], RunSnapshotEvent)
    assert [
        item.payload.event.sequence for item in emitted[1:6] if isinstance(item, RunPersistedEvent)
    ] == [2, 3, 4, 5, 6]
    # 首屏 1 次 + 首批首条触发 1 次 = 2 次；后续 4 条已被 event_cursor=6 覆盖，不再触发快照。
    assert repository.snapshot_calls == 2


def test_stream_still_reconciles_child_forwarded_persisted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父 Run 收到子 Run 转发的 RunPersistedEvent（子序 <= 父游标）时仍必须立即对账。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 60.0)
    now = datetime.now(UTC)

    parent_event = RunEvent(
        id="evt_parent_5",
        run_id="run_stream_test",
        sequence=5,
        type="queued",
        created_at=now,
        payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
    )
    child_event = RunEvent(
        id="evt_child_1",
        run_id="sub_run_child_1",
        sequence=1,
        type="queued",
        created_at=now,
        payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
    )

    class ParentStreamRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.WAITING_FOR_SUBAGENTS, epoch=1)
            self.snapshot_calls = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return RunStreamSnapshot(
                    replace(_run(RunStatus.WAITING_FOR_SUBAGENTS, 1), event_sequence=5),
                    (),
                    (parent_event,),
                )
            return RunStreamSnapshot(
                replace(_run(RunStatus.COMPLETED, 1), event_sequence=5),
                (),
                (parent_event,),
            )

    repository = ParentStreamRepository()
    incoming: list[RunStreamEvent] = [
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunPersistedEventPayload(event=child_event),
        )
    ]
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            item
            async for item in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    emitted = asyncio.run(collect())
    assert repository.snapshot_calls == 2
    assert isinstance(emitted[-1], RunSnapshotEvent)
    assert emitted[-1].payload.status is RunStatus.COMPLETED


def test_redis_publish_uses_zero_transport_sequence_and_enforces_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传输层统一以 stream_sequence=0 发布并严格校验 64KB 单帧上限。"""
    from domain.run.model import RunCompletedPayload

    class RecordingRedis:
        def __init__(self) -> None:
            self.published: list[tuple[str, bytes]] = []

        async def publish(self, channel: str, raw: bytes) -> int:
            self.published.append((channel, raw))
            return 1

    async def scenario() -> None:
        client = RecordingRedis()
        transport = RedisRunTransport(cast(Redis, client))
        tricky_run_id = 'run_"stream_sequence":0'
        tricky_text = ',"stream_sequence":0,"payload":{"fake":1}'
        await transport.publish_text(tricky_run_id, 7, tricky_text, round=2)
        await transport.publish_text(tricky_run_id, 7, "second chunk", round=2)
        await transport.publish_text(tricky_run_id, 8, "new epoch chunk", round=0)

        assert len(client.published) == 3
        first = redis_module._EVENT_ADAPTER.validate_json(client.published[0][1])
        second = redis_module._EVENT_ADAPTER.validate_json(client.published[1][1])
        third = redis_module._EVENT_ADAPTER.validate_json(client.published[2][1])
        assert isinstance(first, RunTextDeltaEvent)
        assert first.run_id == tricky_run_id
        assert first.stream_sequence == 0
        assert first.payload.text == tricky_text
        assert first.payload.round == 2
        assert isinstance(second, RunTextDeltaEvent)
        assert second.stream_sequence == 0
        assert isinstance(third, RunTextDeltaEvent)
        assert third.execution_epoch == 8 and third.stream_sequence == 0

        now = datetime.now(UTC)
        parent_completed = RunEvent(
            id="evt_parent_done",
            run_id=tricky_run_id,
            sequence=3,
            type="completed",
            created_at=now,
            payload=RunCompletedPayload(assistant_message_id="msg_final_1"),
        )
        # 超过单帧 64KB 预算时明确报错，不发送到 Redis。
        monkeypatch.setattr(redis_module, "MAX_REDIS_EVENT_BYTES", 32)
        with pytest.raises(ValueError, match="run_stream_event_exceeds_redis_limit"):
            await transport.publish_persisted("run_oversize", 1, parent_completed)

    asyncio.run(scenario())


def test_redis_split_text_chunks_utf8_and_json_escape_budget() -> None:
    """8192 字符定长切片无损拼接 UTF-8 与高密度转义字符，且单帧严格 <= 64KB。"""
    sse_prefix_reserve = len(b"event: text_delta\nid: 99999999999999999999\ndata: \n\n")
    assert RedisRunTransport._split_text_chunks("") == [""]
    small = 'hello 世界🙂\x00"\\' * 50
    assert RedisRunTransport._split_text_chunks(small) == [small]

    # 超长多字节文本按字符边界切分，无损拼接且每片满足 64KB 帧上限。
    large = "界🙂" * 20_000
    chunks = RedisRunTransport._split_text_chunks(large)
    assert len(chunks) > 1
    assert "".join(chunks) == large
    for chunk in chunks:
        fitted = RunTextDeltaEvent(
            run_id="run_big",
            execution_epoch=9,
            stream_sequence=9_999_999_999_999_999_999,
            payload=RunTextDeltaPayload(text=chunk, round=4),
        )
        fitted_len = len(fitted.model_dump_json(exclude_none=True).encode("utf-8"))
        assert fitted_len + sse_prefix_reserve <= redis_module.MAX_REDIS_EVENT_BYTES

    # 最坏情况每字符 6 字节 JSON 转义（\u0000）同样严格满足 64KB 上限。
    heavy_escape = '"\x00\\\n' * 12_000
    escaped_chunks = RedisRunTransport._split_text_chunks(heavy_escape)
    assert len(escaped_chunks) > 1
    assert "".join(escaped_chunks) == heavy_escape
    for chunk in escaped_chunks:
        fitted = RunTextDeltaEvent(
            run_id="run_esc",
            execution_epoch=2,
            stream_sequence=9_999_999_999_999_999_999,
            payload=RunTextDeltaPayload(text=chunk, round=1),
        )
        fitted_len = len(fitted.model_dump_json(exclude_none=True).encode("utf-8"))
        assert fitted_len + sse_prefix_reserve <= redis_module.MAX_REDIS_EVENT_BYTES


def test_redis_transport_direct_pubsub_lifecycle_and_invalid_payload_filtering() -> None:
    """每个 SSE 订阅直连独立 Pub/Sub，过滤损坏与超限消息，退出时干净退订并关闭连接。"""

    class TrackedPubSub:
        def __init__(self) -> None:
            self.subscribed: list[str] = []
            self.unsubscribed: list[str] = []
            self.closed = False
            self.queue: deque[dict[str, object]] = deque()

        async def subscribe(self, channel_name: str) -> None:
            self.subscribed.append(channel_name)

        async def unsubscribe(self, channel_name: str) -> None:
            self.unsubscribed.append(channel_name)

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            if self.queue:
                return self.queue.popleft()
            return None

        async def aclose(self) -> None:
            self.closed = True

    class TrackedRedis:
        def __init__(self) -> None:
            self.pubsubs: list[TrackedPubSub] = []
            self.client_closed = 0

        def pubsub(self) -> TrackedPubSub:
            ps = TrackedPubSub()
            self.pubsubs.append(ps)
            return ps

        async def aclose(self) -> None:
            self.client_closed += 1

    async def scenario() -> None:
        client = TrackedRedis()
        transport = RedisRunTransport(cast(Redis, client), owns_client=False)
        chan = RedisRunTransport._channel("run_direct")
        valid = RunTextDeltaEvent(
            run_id="run_direct",
            execution_epoch=1,
            stream_sequence=0,
            payload=RunTextDeltaPayload(text="ok"),
        )
        async with transport.subscribe("run_direct") as sub1:
            async with transport.subscribe("run_direct") as sub2:
                assert len(client.pubsubs) == 2
                ps1, ps2 = client.pubsubs
                assert ps1.subscribed == [chan]
                assert ps2.subscribed == [chan]
                # 投递 subscribe 控制帧、损坏 JSON、超限载荷和合法事件：仅返回合法事件。
                ps1.queue.append({"type": "subscribe", "channel": chan, "data": 1})
                ps1.queue.append({"type": "message", "channel": chan, "data": b"not-json"})
                ps1.queue.append(
                    {
                        "type": "message",
                        "channel": chan,
                        "data": b"x" * (redis_module.MAX_REDIS_EVENT_BYTES + 1),
                    }
                )
                ps1.queue.append(
                    {
                        "type": "message",
                        "channel": chan,
                        "data": valid.model_dump_json().encode("utf-8"),
                    }
                )
                assert await sub1.get(timeout=0.5) == valid
                assert await sub2.get(timeout=0.05) is None
            assert client.pubsubs[1].unsubscribed == [chan]
            assert client.pubsubs[1].closed is True
            assert client.pubsubs[0].closed is False
        assert client.pubsubs[0].unsubscribed == [chan]
        assert client.pubsubs[0].closed is True
        await transport.aclose()
        assert client.client_closed == 0

        owned_client = TrackedRedis()
        owned = RedisRunTransport(cast(Redis, owned_client), owns_client=True)
        await owned.aclose()
        assert owned_client.client_closed == 1

    asyncio.run(scenario())


def test_redis_subscription_aclose_always_closes_pubsub_on_oserror_and_cancellation() -> None:
    """unsubscribe 抛 OSError 或被取消时仍必须执行 pubsub.aclose()，避免连接泄漏。"""

    # 场景 1：unsubscribe 抛 OSError 时仍关闭底层 pubsub。
    async def scenario_oserror() -> None:
        closed = False

        class OsErrorPubSub:
            async def subscribe(self, channel_name: str) -> None:
                del channel_name

            async def unsubscribe(self, channel_name: str) -> None:
                del channel_name
                raise OSError("broken pipe")

            async def aclose(self) -> None:
                nonlocal closed
                closed = True

        class FakeRedis:
            def pubsub(self) -> OsErrorPubSub:
                return OsErrorPubSub()

        transport = RedisRunTransport(cast(Redis, FakeRedis()), owns_client=False)
        async with transport.subscribe("run_oserror_close"):
            pass
        assert closed is True

    # 场景 2：unsubscribe 阻塞期间被 CancelledError 取消，仍必须在 finally 中执行 pubsub.aclose()。
    async def scenario_cancelled() -> None:
        entered_unsubscribe = asyncio.Event()
        closed = False

        class BlockingUnsubscribePubSub:
            async def subscribe(self, channel_name: str) -> None:
                del channel_name

            async def unsubscribe(self, channel_name: str) -> None:
                del channel_name
                entered_unsubscribe.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                nonlocal closed
                closed = True

        class FakeRedis:
            def pubsub(self) -> BlockingUnsubscribePubSub:
                return BlockingUnsubscribePubSub()

        transport = RedisRunTransport(cast(Redis, FakeRedis()), owns_client=False)
        cm = transport.subscribe("run_cancel_close")
        subscription = await cm.__aenter__()
        close_task = asyncio.create_task(subscription.aclose())
        await entered_unsubscribe.wait()
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert closed is True

    asyncio.run(scenario_oserror())
    asyncio.run(scenario_cancelled())


def test_redis_transport_translates_timeout_on_subscribe_and_publish() -> None:
    """subscribe 与 publish 超时均统一转译为 RunEventTransportError，且 subscribe 清理 pubsub。"""

    async def scenario() -> None:
        closed = False

        class TimeoutPubSub:
            async def subscribe(self, channel_name: str) -> None:
                del channel_name
                raise TimeoutError("subscribe timed out")

            async def aclose(self) -> None:
                nonlocal closed
                closed = True

        class TimeoutRedis:
            def pubsub(self) -> TimeoutPubSub:
                return TimeoutPubSub()

            async def publish(self, channel: str, raw: bytes) -> int:
                del channel, raw
                raise TimeoutError("publish timed out")

        transport = RedisRunTransport(cast(Redis, TimeoutRedis()), owns_client=False)
        with pytest.raises(RunEventTransportError, match="run_stream_subscribe_failed") as sub_err:
            async with transport.subscribe("run_sub_timeout"):
                pass
        assert isinstance(sub_err.value.__cause__, TimeoutError)
        assert closed is True

        with pytest.raises(RunEventTransportError, match="run_stream_publish_failed") as pub_err:
            await transport.publish_text("run_pub_timeout", 1, "delta")
        assert isinstance(pub_err.value.__cause__, TimeoutError)

    asyncio.run(scenario())


def test_stream_cancel_during_subscribe_aenter_never_calls_aexit() -> None:
    """SSE 建连阶段在 __aenter__ 被取消时，绝不对未成功进入的异步上下文管理器调用 __aexit__。"""

    async def scenario() -> None:
        entered_connect = asyncio.Event()
        aexit_calls = 0

        class SlowEnterCM:
            async def __aenter__(self) -> RunEventSubscription:
                entered_connect.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def __aexit__(self, *args: object) -> None:
                nonlocal aexit_calls
                del args
                aexit_calls += 1

        class SlowConnectTransport:
            def subscribe(self, run_id: str) -> SlowEnterCM:
                del run_id
                return SlowEnterCM()

        service = RunStreamService(
            cast(RunRepository, _FakeRepository(status=RunStatus.RUNNING, epoch=1)),
            cast(Authenticator, _FakeAuthenticator()),
            cast(RunEventTransport, SlowConnectTransport()),
        )

        async def consume() -> None:
            async for _ in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            ):
                pass

        task = asyncio.create_task(consume())
        await entered_connect.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert aexit_calls == 0

    asyncio.run(scenario())


def test_get_stream_snapshot_trims_outside_transaction_and_advances_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_stream_snapshot 在释放事务与连接后再执行消息与 512KB 快照裁剪。"""
    from collections.abc import Sequence
    from types import SimpleNamespace

    from adapter.run.model import RunModel
    from adapter.run.repository import context_events as ctx_module
    from adapter.run.repository.context_events import RunContextEventStore
    from domain.run.model import RunSnapshotEvent

    now = datetime.now(UTC)
    tx_active = False
    session_active = False
    tx_active_during_snapshot_dump: list[bool] = []
    session_active_during_snapshot_dump: list[bool] = []

    original_snapshot_dump = RunSnapshotEvent.model_dump_json

    def spy_snapshot_dump(self: RunSnapshotEvent, *args: object, **kwargs: object) -> str:
        tx_active_during_snapshot_dump.append(tx_active)
        session_active_during_snapshot_dump.append(session_active)
        return original_snapshot_dump(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RunSnapshotEvent, "model_dump_json", spy_snapshot_dump)
    # RunStreamMessage 同时携带 text 与自包含 parts，2560B 预算可保留 msg_3 并裁掉旧 msg_2。
    monkeypatch.setattr(ctx_module, "_STREAM_SNAPSHOT_BYTES", 2560)

    run_row = RunModel(
        id="run_snap_1",
        session_id="ses_snap_1",
        owner_id="usr_snap_1",
        status="running",
        trigger_message_id="msg_1",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile_1",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=2,
        execution_epoch=1,
        worker_id="worker-1",
        lease_expires_at=now + timedelta(seconds=30),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(seconds=60),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        report_message_id=None,
        usage={},
        compression_policy_version_id=None,
        model_snapshot_json={
            "id": "profile_1",
            "name": "Stream Model",
            "protocol": "chat_completions",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-4o-mini",
            "context_window_tokens": 128000,
            "tokenizer_encoding": "cl100k_base",
            "supports_stream": True,
            "supports_tools": True,
            "supports_parallel_tool_calls": False,
            "max_output_tokens": 4096,
            "request_timeout_seconds": 30,
            "execution_timeout_seconds": 120,
            "max_concurrency": 4,
            "temperature": None,
            "scenario_label": None,
            "supports_vision": False,
            "version": 1,
        },
        tool_snapshot=None,
        auth_session_id=None,
        llm_request_count=0,
        skill_snapshot=None,
        input_file_ids=[],
        knowledge_base_ids=None,
        parent_run_id=None,
        root_run_id="run_snap_1",
        parent_tool_call_id=None,
        subagent_profile_id=None,
        subagent_slug=None,
        subagent_name=None,
        subagent_system_prompt=None,
        max_llm_requests=None,
        timeout_seconds=None,
        depth=1,  # 子 Run 跳过 subagent metadata 查询，专注测试快照事务边界与裁剪
    )

    msg_rows = [
        SimpleNamespace(
            id="msg_3",
            session_id="ses_snap_1",
            role="assistant",
            status="completed",
            sequence=3,
            parts=[{"type": "text", "text": "B" * 600}],
            reply_to_message_id=None,
            run_id="run_snap_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id="msg_1",
        ),
        SimpleNamespace(
            id="msg_2",
            session_id="ses_snap_1",
            role="assistant",
            status="completed",
            sequence=2,
            parts=[{"type": "text", "text": "A" * 1700}],
            reply_to_message_id=None,
            run_id="run_snap_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id="msg_1",
        ),
    ]
    evt_rows = [
        SimpleNamespace(
            id="evt_1",
            run_id="run_snap_1",
            session_id="ses_snap_1",
            sequence=1,
            type="queued",
            created_at=now,
            payload={"trigger_message_id": "msg_1"},
        ),
        SimpleNamespace(
            id="evt_2",
            run_id="run_snap_1",
            session_id="ses_snap_1",
            sequence=2,
            type="queued",
            created_at=now,
            payload={"trigger_message_id": "msg_1"},
        ),
    ]

    class _FakeScalarResult:
        def __init__(self, items: Sequence[object]) -> None:
            self._items = list(items)

        def all(self) -> list[object]:
            return self._items

    class _FakeBegin:
        async def __aenter__(self) -> None:
            nonlocal tx_active
            tx_active = True

        async def __aexit__(self, *args: object) -> None:
            nonlocal tx_active
            del args
            tx_active = False

    executed_statements: list[str] = []

    class _FakeSession:
        def __init__(self) -> None:
            self._scalars_call = 0

        def begin(self) -> _FakeBegin:
            return _FakeBegin()

        async def execute(self, stmt: object) -> None:
            executed_statements.append(str(stmt))

        async def scalar(self, stmt: object) -> object:
            del stmt
            return run_row

        async def scalars(self, stmt: object) -> _FakeScalarResult:
            del stmt
            self._scalars_call += 1
            if self._scalars_call == 1:
                return _FakeScalarResult(msg_rows)
            return _FakeScalarResult(evt_rows)

        async def __aenter__(self) -> _FakeSession:
            nonlocal session_active
            session_active = True
            return self

        async def __aexit__(self, *args: object) -> None:
            nonlocal session_active
            del args
            session_active = False

    repo = RunContextEventStore(cast(Any, lambda: _FakeSession()))

    snapshot = asyncio.run(repo.get_stream_snapshot("ses_snap_1", "usr_snap_1", "run_snap_1"))
    assert snapshot is not None
    assert executed_statements == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"]
    # 步骤 7 序列化全部发生在事务与会话完全退出之后。
    assert tx_active_during_snapshot_dump
    assert all(not active for active in tx_active_during_snapshot_dump)
    assert all(not active for active in session_active_during_snapshot_dump)
    # 较旧的超长消息 msg_2 被裁掉，保留 msg_3 且游标推进至 2 + 1 = 3，同时携带自包含扩展字段。
    assert [message.id for message in snapshot.messages] == ["msg_3"]
    assert snapshot.messages[0].session_id == "ses_snap_1"
    assert snapshot.messages[0].run_id == "run_snap_1"
    assert snapshot.messages[0].parts == ({"type": "text", "text": "B" * 600},)
    assert snapshot.messages[0].completed_at == now
    assert snapshot.messages_complete is False
    assert snapshot.messages_before_sequence == 3
    assert [event.sequence for event in snapshot.events] == [1, 2]


def test_public_sse_stream_yields_bytes_and_rejects_oversize_frame() -> None:
    """公开 SSE 响应直接产出 UTF-8 bytes，且超限帧仍抛 run_stream_event_exceeds_size_limit。"""
    session_id = "session_bytes_stream"
    run_id = "run_bytes_stream"

    class OversizeService:
        async def authorize(self, session: str, owner: str, run: str) -> bool:
            return session == session_id and run == run_id and owner == "user_stream_test"

        async def events(
            self,
            session: str,
            owner: str,
            run: str,
            token: str,
            *,
            last_event_id: str | None = None,
        ) -> AsyncIterator[RunStreamEvent]:
            del session, owner, token, last_event_id
            yield RunTextDeltaEvent(
                run_id=run,
                execution_epoch=1,
                stream_sequence=1,
                payload=RunTextDeltaPayload(text="x" * (64 * 1024)),
            )

    app = FastAPI()
    app.state.authenticator = _FakeAuthenticator()
    app.include_router(
        build_run_router(cast(RunService, object()), cast(RunStreamService, OversizeService()))
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError, match="run_stream_event_exceeds_size_limit"):
            client.get(
                f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
                cookies={"athena_session": "valid-token"},
            )


def test_stream_direct_forwards_contiguous_progress_events_and_reconciles_on_gap_or_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """序号连续的纯进展型事件直接转发零查库；缺口与终态仍回查权威快照并即时关闭流。"""
    import app.run.stream as stream_module
    from domain.run.model import (
        RunCompletedPayload,
        RunCompressionStartedPayload,
        RunStartedPayload,
        RunToolCallFinishedPayload,
        RunToolCallStartedPayload,
    )
    from domain.tool import ToolCallStatus

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 60.0)
    now = datetime.now(UTC)
    final_msg = RunStreamMessage(
        id="msg_assistant_final",
        role="assistant",
        status="completed",
        text="已完成分析",
        sequence=2,
        created_at=now,
        session_id="session_stream_test",
        run_id="run_stream_test",
        reply_to_message_id="message_stream_test",
        parts=(
            {"type": "text", "text": "已完成分析"},
            {
                "type": "file_ref",
                "file_id": "file_report_1",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1024,
            },
        ),
        completed_at=now,
    )
    all_events: list[RunEvent] = [
        RunEvent(
            id="evt_1",
            run_id="run_stream_test",
            sequence=1,
            type="queued",
            created_at=now,
            payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
        ),
        RunEvent(
            id="evt_2",
            run_id="run_stream_test",
            sequence=2,
            type="started",
            created_at=now,
            payload=RunStartedPayload(execution_epoch=1, worker_id="worker-1"),
        ),
        RunEvent(
            id="evt_3",
            run_id="run_stream_test",
            sequence=3,
            type="tool_call_started",
            created_at=now,
            payload=RunToolCallStartedPayload(call_id="tc_1", ordinal=1, round=0),
        ),
        RunEvent(
            id="evt_4",
            run_id="run_stream_test",
            sequence=4,
            type="tool_call_finished",
            created_at=now,
            payload=RunToolCallFinishedPayload(
                call_id="tc_1", ordinal=1, status=ToolCallStatus.COMPLETED, round=0
            ),
        ),
        RunEvent(
            id="evt_5",
            run_id="run_stream_test",
            sequence=5,
            type="compression_started",
            created_at=now,
            payload=RunCompressionStartedPayload(operation_id="cmp_1"),
        ),
        RunEvent(
            id="evt_6",
            run_id="run_stream_test",
            sequence=6,
            type="compression_completed",
            created_at=now,
            payload=RunCompressionCompletedPayload(
                operation_id="cmp_1", summary_id="sum_1", covered_through_sequence=1, batch_count=1
            ),
        ),
        RunEvent(
            id="evt_7",
            run_id="run_stream_test",
            sequence=7,
            type="tool_call_started",
            created_at=now,
            payload=RunToolCallStartedPayload(call_id="tc_2", ordinal=2, round=1),
        ),
        RunEvent(
            id="evt_8",
            run_id="run_stream_test",
            sequence=8,
            type="tool_call_finished",
            created_at=now,
            payload=RunToolCallFinishedPayload(
                call_id="tc_2", ordinal=2, status=ToolCallStatus.COMPLETED, round=1
            ),
        ),
        RunEvent(
            id="evt_9",
            run_id="run_stream_test",
            sequence=9,
            type="completed",
            created_at=now,
            payload=RunCompletedPayload(assistant_message_id="msg_assistant_final"),
        ),
    ]

    class ProgressRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshot_calls = 0
            self.list_events_calls = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return RunStreamSnapshot(
                    replace(_run(RunStatus.RUNNING, 1), event_sequence=2),
                    (),
                    tuple(all_events[:2]),
                )
            if self.snapshot_calls == 2:
                return RunStreamSnapshot(
                    replace(_run(RunStatus.RUNNING, 1), event_sequence=8),
                    (),
                    tuple(all_events[:8]),
                )
            return RunStreamSnapshot(
                replace(_run(RunStatus.COMPLETED, 1), event_sequence=9),
                (final_msg,),
                tuple(all_events),
                assistant_message_id="msg_assistant_final",
            )

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
            self.list_events_calls += 1
            upper = through_sequence or len(all_events)
            return tuple(evt for evt in all_events if after_sequence < evt.sequence <= upper)[
                :limit
            ]

    incoming: list[RunStreamEvent] = [
        # 序号 3..6 连续纯进展型事件：全部直接转发，不触发 DB 快照。
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunPersistedEventPayload(event=all_events[2]),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=2,
            payload=RunPersistedEventPayload(event=all_events[3]),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=3,
            payload=RunPersistedEventPayload(event=all_events[4]),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=4,
            payload=RunPersistedEventPayload(event=all_events[5]),
        ),
        # 跳过 seq=7 直接到达 seq=8：触发第 2 次快照对账并补齐 7、8。
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=5,
            payload=RunPersistedEventPayload(event=all_events[7]),
        ),
        # 终态 completed（seq=9）：触发第 3 次快照对账、推送自包含终态快照并立即关闭 SSE 流。
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=6,
            payload=RunPersistedEventPayload(event=all_events[8]),
        ),
    ]
    repository = ProgressRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            item
            async for item in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    emitted = asyncio.run(collect())
    # 仅首屏 1 次 + 缺口对账 1 次 + 终态对账 1 次 = 3 次（4 条连续纯进展型事件零快照查库）。
    assert repository.snapshot_calls == 3
    # 缺口对账与终态对账各分页补齐 1 次。
    assert repository.list_events_calls == 2
    persisted_seqs = [
        item.payload.event.sequence for item in emitted if isinstance(item, RunPersistedEvent)
    ]
    assert persisted_seqs == [3, 4, 5, 6, 7, 8, 9]
    terminal = emitted[-1]
    assert isinstance(terminal, RunSnapshotEvent)
    assert terminal.payload.status is RunStatus.COMPLETED
    assert terminal.payload.assistant_message_id == "msg_assistant_final"
    assert terminal.payload.messages[0].parts is not None
    assert terminal.payload.messages[0].parts[1]["type"] == "file_ref"


def test_stream_reconciles_on_subagent_and_waiting_approval_even_when_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子智能体状态变更与等待审批事件即使序号连续也不直传，必须回查 DB 更新卡片投影并关闭流。"""
    import app.run.stream as stream_module
    from domain.run.model import (
        RunSubagentStatusChangedPayload,
        RunWaitingForApprovalPayload,
        SubagentTaskSummary,
    )

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 60.0)
    now = datetime.now(UTC)
    evt_1 = RunEvent(
        id="evt_1",
        run_id="run_stream_test",
        sequence=1,
        type="queued",
        created_at=now,
        payload=RunQueuedPayload(trigger_message_id="message_stream_test"),
    )
    evt_2 = RunEvent(
        id="evt_2",
        run_id="run_stream_test",
        sequence=2,
        type="subagent_status_changed",
        created_at=now,
        payload=RunSubagentStatusChangedPayload(
            sub_run_id="sub_1",
            subagent_slug="research",
            subagent_name="Research",
            status=RunStatus.RUNNING,
        ),
    )
    evt_3 = RunEvent(
        id="evt_3",
        run_id="run_stream_test",
        sequence=3,
        type="waiting_for_approval",
        created_at=now,
        payload=RunWaitingForApprovalPayload(
            batch_message_id="exec_batch_1", pending_call_ids=("tc_approval_1",)
        ),
    )
    sub_summary = SubagentTaskSummary(
        sub_run_id="sub_1",
        parent_tool_call_id="ptc_1",
        subagent_slug="research",
        subagent_name="Research",
        status=RunStatus.RUNNING,
        created_at=now,
    )

    class ProjectionRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1)
            self.snapshot_calls = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return RunStreamSnapshot(
                    replace(_run(RunStatus.RUNNING, 1), event_sequence=1), (), (evt_1,)
                )
            if self.snapshot_calls == 2:
                return RunStreamSnapshot(
                    replace(_run(RunStatus.RUNNING, 1), event_sequence=2),
                    (),
                    (evt_1, evt_2),
                    subagent_tasks=(sub_summary,),
                )
            return RunStreamSnapshot(
                replace(_run(RunStatus.WAITING_FOR_APPROVAL, 1), event_sequence=3),
                (),
                (evt_1, evt_2, evt_3),
                subagent_tasks=(sub_summary,),
            )

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
            upper = through_sequence or 3
            return tuple(e for e in (evt_1, evt_2, evt_3) if after_sequence < e.sequence <= upper)[
                :limit
            ]

    incoming: list[RunStreamEvent] = [
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunPersistedEventPayload(event=evt_2),
        ),
        RunPersistedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=2,
            payload=RunPersistedEventPayload(event=evt_3),
        ),
    ]
    repository = ProjectionRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        return [
            item
            async for item in service.events(
                "session_stream_test", "user_stream_test", "run_stream_test", "token"
            )
        ]

    emitted = asyncio.run(collect())
    # 首屏 + subagent_status_changed 对账 + waiting_for_approval 对账 = 3 次快照。
    assert repository.snapshot_calls == 3
    snapshots = [item for item in emitted if isinstance(item, RunSnapshotEvent)]
    assert len(snapshots) == 3
    assert len(snapshots[1].payload.subagent_tasks) == 1
    assert snapshots[2].payload.status is RunStatus.WAITING_FOR_APPROVAL


def test_get_stream_snapshot_self_contained_terminal_fields_and_512kb_terminal_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态快照填充自包含消息与 assistant_message_id，校验必填字段契约并优先裁事件保护终态消息。"""
    from collections.abc import Sequence
    from types import SimpleNamespace

    from pydantic import ValidationError

    from adapter.run.model import RunModel
    from adapter.run.repository import context_events as ctx_module
    from adapter.run.repository.context_events import RunContextEventStore

    now = datetime.now(UTC)
    # 1. 验证 RunStreamMessage 收紧后的契约：session_id 与 parts 必填，可空元信息字段仍允许省略。
    missing_required_json = (
        '{"type":"snapshot","run_id":"r1","execution_epoch":1,"stream_sequence":0,'
        '"payload":{"status":"completed","event_sequence":2,"messages":[{"id":"m1",'
        '"role":"assistant","status":"completed","text":"legacy","sequence":2,'
        '"created_at":"2026-01-01T00:00:00Z"}],"events":[]}}'
    )
    with pytest.raises(ValidationError):
        redis_module._EVENT_ADAPTER.validate_json(missing_required_json)

    minimal_snapshot_json = (
        '{"type":"snapshot","run_id":"r1","execution_epoch":1,"stream_sequence":0,'
        '"payload":{"status":"completed","event_sequence":2,"messages":[{"id":"m1",'
        '"session_id":"s1","role":"assistant","status":"completed","text":"ok",'
        '"parts":[{"type":"text","text":"ok"}],"sequence":2,'
        '"created_at":"2026-01-01T00:00:00Z"}],"events":[]}}'
    )
    parsed_minimal = redis_module._EVENT_ADAPTER.validate_json(minimal_snapshot_json)
    assert isinstance(parsed_minimal, RunSnapshotEvent)
    assert parsed_minimal.payload.assistant_message_id is None
    assert parsed_minimal.payload.messages[0].session_id == "s1"
    assert parsed_minimal.payload.messages[0].parts == ({"type": "text", "text": "ok"},)
    assert parsed_minimal.payload.messages[0].run_id is None
    assert parsed_minimal.payload.messages[0].reply_to_message_id is None
    assert parsed_minimal.payload.messages[0].completed_at is None

    # 2. 验证 completed 终态填充完整 parts（含 file_ref）、assistant_message_id 与裁剪保护。
    run_row = RunModel(
        id="run_term_1",
        session_id="ses_term_1",
        owner_id="usr_term_1",
        status="completed",
        trigger_message_id="msg_user_1",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile_1",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=4,
        execution_epoch=1,
        worker_id=None,
        lease_expires_at=None,
        execution_started_at=now,
        execution_deadline_at=now + timedelta(seconds=60),
        queued_at=now,
        first_started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        report_message_id=None,
        usage={},
        compression_policy_version_id=None,
        model_snapshot_json={
            "id": "profile_1",
            "name": "Stream Model",
            "protocol": "chat_completions",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-4o-mini",
            "context_window_tokens": 128000,
            "tokenizer_encoding": "cl100k_base",
            "supports_stream": True,
            "supports_tools": True,
            "supports_parallel_tool_calls": False,
            "max_output_tokens": 4096,
            "request_timeout_seconds": 30,
            "execution_timeout_seconds": 120,
            "max_concurrency": 4,
            "temperature": None,
            "scenario_label": None,
            "supports_vision": False,
            "version": 1,
        },
        tool_snapshot=None,
        auth_session_id=None,
        llm_request_count=1,
        skill_snapshot=None,
        input_file_ids=[],
        knowledge_base_ids=None,
        parent_run_id=None,
        root_run_id="run_term_1",
        parent_tool_call_id=None,
        subagent_profile_id=None,
        subagent_slug=None,
        subagent_name=None,
        subagent_system_prompt=None,
        max_llm_requests=None,
        timeout_seconds=None,
        depth=1,
    )
    msg_rows = [
        SimpleNamespace(
            id="msg_final_assistant",
            session_id="ses_term_1",
            role="assistant",
            status="completed",
            sequence=2,
            parts=[
                {"type": "text", "text": "R" * 450},
                {
                    "type": "file_ref",
                    "file_id": "file_1",
                    "filename": "summary.csv",
                    "mime_type": "text/csv",
                    "size_bytes": 512,
                },
            ],
            reply_to_message_id="msg_user_1",
            run_id="run_term_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id="msg_user_1",
        ),
        SimpleNamespace(
            id="msg_user_1",
            session_id="ses_term_1",
            role="user",
            status="completed",
            sequence=1,
            parts=[{"type": "text", "text": "U" * 500}],
            reply_to_message_id=None,
            run_id="run_term_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id="msg_user_1",
        ),
    ]
    evt_rows = [
        SimpleNamespace(
            id=f"evt_{seq}",
            run_id="run_term_1",
            session_id="ses_term_1",
            sequence=seq,
            type="queued" if seq < 4 else "completed",
            created_at=now,
            payload=(
                {"trigger_message_id": "msg_user_1"}
                if seq < 4
                else {"assistant_message_id": "msg_final_assistant"}
            ),
        )
        for seq in range(1, 5)
    ]

    class _FakeScalarResult:
        def __init__(self, items: Sequence[object]) -> None:
            self._items = list(items)

        def all(self) -> list[object]:
            return self._items

    class _FakeBegin:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            del args

    executed_statements: list[str] = []

    class _FakeSession:
        def __init__(self) -> None:
            self._scalars_call = 0

        def begin(self) -> _FakeBegin:
            return _FakeBegin()

        async def execute(self, stmt: object) -> None:
            executed_statements.append(str(stmt))

        async def scalar(self, stmt: object) -> object:
            del stmt
            return run_row

        async def scalars(self, stmt: object) -> _FakeScalarResult:
            del stmt
            self._scalars_call += 1
            if self._scalars_call == 1:
                return _FakeScalarResult(msg_rows)
            return _FakeScalarResult(evt_rows)

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    repo = RunContextEventStore(cast(Any, lambda: _FakeSession()))

    # 紧凑预算：全量消息+事件超限，裁掉旧用户消息与尾部事件后可完整保留最终助手消息。
    monkeypatch.setattr(ctx_module, "_STREAM_SNAPSHOT_BYTES", 2000)
    snapshot = asyncio.run(repo.get_stream_snapshot("ses_term_1", "usr_term_1", "run_term_1"))
    assert snapshot is not None
    assert executed_statements == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"]
    assert snapshot.assistant_message_id == "msg_final_assistant"
    assert [m.id for m in snapshot.messages] == ["msg_final_assistant"]
    final_projected = snapshot.messages[0]
    assert final_projected.session_id == "ses_term_1"
    assert final_projected.run_id == "run_term_1"
    assert final_projected.reply_to_message_id == "msg_user_1"
    assert final_projected.completed_at == now
    assert final_projected.parts == (
        {"type": "text", "text": "R" * 450},
        {
            "type": "file_ref",
            "file_id": "file_1",
            "filename": "summary.csv",
            "mime_type": "text/csv",
            "size_bytes": 512,
        },
    )
    assert snapshot.messages_complete is False
    assert snapshot.messages_before_sequence == 2
    assert snapshot.events_complete is False
    assert len(snapshot.events) < 4


def test_sqlalchemy_completed_run_snapshot_beyond_100_events_and_get_details_assistant_id() -> None:
    """超过 100 条事件的 completed 快照与 get_details 均准确填充 assistant_message_id。"""
    from pg_unit_harness import create_pg_unit_db

    from adapter.auth.model import UserModel
    from adapter.llm.model import ModelProfileVersionModel
    from adapter.message.model import ConversationMessageModel
    from adapter.message.repository import SqlAlchemyMessageRepository
    from adapter.run.model import RunEventModel, RunModel
    from adapter.run.repository import SqlAlchemyRunRepository
    from adapter.session.model import SessionModel
    from adapter.system_prompt.model import SystemPromptVersionModel

    async def scenario() -> None:
        engine, factory = await create_pg_unit_db()
        now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
        try:
            async with factory() as session, session.begin():
                session.add_all(
                    [
                        UserModel(
                            id="usr_sq_1",
                            email="sq@example.test",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            created_at=now,
                            updated_at=now,
                        ),
                        ModelProfileVersionModel(
                            id="prof_1",
                            name="Stream Model",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="gpt-test",
                            api_key_ciphertext=b"ciphertext",
                            api_key_nonce=b"0123456789ab",
                            api_key_key_version=1,
                            context_window_tokens=128000,
                            tokenizer_encoding="cl100k_base",
                            max_output_tokens=4096,
                            request_timeout_seconds=30,
                            execution_timeout_seconds=120,
                            max_concurrency=4,
                            created_at=now,
                            updated_at=now,
                        ),
                        SystemPromptVersionModel(
                            id=TEST_MAIN_PROMPT_ID,
                            kind="main",
                            content="main prompt",
                            created_at=now,
                        ),
                        SystemPromptVersionModel(
                            id=TEST_COMPRESSION_PROMPT_ID,
                            kind="compression",
                            content="compression prompt",
                            created_at=now,
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    SessionModel(
                        id="ses_sq_1",
                        owner_id="usr_sq_1",
                        title="PostgreSQL 终态快照回归",
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                # runs.trigger_message_id 与 conversation_messages.run_id 均为 DEFERRABLE，
                # 同事务内先 flush 消息与 Run 再插入关联的 run_events。
                session.add_all(
                    [
                        ConversationMessageModel(
                            id="msg_sq_user",
                            session_id="ses_sq_1",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "请生成报告"}],
                            reply_to_message_id=None,
                            run_id="run_sq_1",
                            context_turn_id="msg_sq_user",
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_sq_final",
                            session_id="ses_sq_1",
                            role="assistant",
                            status="completed",
                            sequence=2,
                            parts=[
                                {"type": "text", "text": "报告已生成"},
                                {
                                    "type": "file_ref",
                                    "file_id": "file_sq_1",
                                    "filename": "out.csv",
                                    "mime_type": "text/csv",
                                    "size_bytes": 2048,
                                },
                            ],
                            reply_to_message_id="msg_sq_user",
                            run_id="run_sq_1",
                            context_turn_id="msg_sq_user",
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                    ]
                )
                session.add(
                    RunModel(
                        id="run_sq_1",
                        session_id="ses_sq_1",
                        owner_id="usr_sq_1",
                        status="completed",
                        trigger_message_id="msg_sq_user",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id="prof_1",
                        model_snapshot_json={
                            "id": "prof_1",
                            "name": "Stream Model",
                            "protocol": "chat_completions",
                            "base_url": "https://llm.example.test/v1",
                            "model": "gpt-test",
                            "context_window_tokens": 128000,
                            "tokenizer_encoding": "cl100k_base",
                            "supports_stream": True,
                            "supports_tools": True,
                            "supports_parallel_tool_calls": False,
                            "max_output_tokens": 4096,
                            "request_timeout_seconds": 30,
                            "execution_timeout_seconds": 120,
                            "max_concurrency": 4,
                            "temperature": None,
                            "scenario_label": None,
                            "supports_vision": False,
                            "version": 1,
                        },
                        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
                        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
                        prompt_template_version="run-v2",
                        event_sequence=105,
                        execution_epoch=1,
                        queued_at=now,
                        first_started_at=now,
                        finished_at=now,
                        created_at=now,
                        updated_at=now,
                        usage={},
                        input_file_ids=[],
                        depth=0,
                    )
                )
                await session.flush()
                # 插入 105 条事件，其中 completed 位于首屏 100 条之外（sequence=105）。
                for seq in range(1, 105):
                    session.add(
                        RunEventModel(
                            id=f"evt_sq_{seq}",
                            run_id="run_sq_1",
                            session_id="ses_sq_1",
                            sequence=seq,
                            type="queued",
                            created_at=now,
                            payload={"type": "queued", "trigger_message_id": "msg_sq_user"},
                        )
                    )
                session.add(
                    RunEventModel(
                        id="evt_sq_105",
                        run_id="run_sq_1",
                        session_id="ses_sq_1",
                        sequence=105,
                        type="completed",
                        created_at=now,
                        payload={"type": "completed", "assistant_message_id": "msg_sq_final"},
                    )
                )

            repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            snapshot = await repo.get_stream_snapshot("ses_sq_1", "usr_sq_1", "run_sq_1")
            assert snapshot is not None
            assert len(snapshot.events) == 100
            assert snapshot.events_complete is False
            assert snapshot.events_after_sequence == 100
            assert snapshot.assistant_message_id == "msg_sq_final"
            assert [m.id for m in snapshot.messages] == ["msg_sq_user", "msg_sq_final"]
            final_msg = snapshot.messages[-1]
            assert final_msg.session_id == "ses_sq_1"
            assert final_msg.run_id == "run_sq_1"
            assert final_msg.reply_to_message_id == "msg_sq_user"
            assert final_msg.completed_at == now
            assert final_msg.parts == (
                {"type": "text", "text": "报告已生成"},
                {
                    "type": "file_ref",
                    "file_id": "file_sq_1",
                    "filename": "out.csv",
                    "mime_type": "text/csv",
                    "size_bytes": 2048,
                },
            )

            details = await repo.get_details("ses_sq_1", "usr_sq_1", "run_sq_1")
            assert details is not None
            assert details.assistant_message_id == "msg_sq_final"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_stream_emits_new_epoch_snapshot_after_higher_epoch_delta_advances_current_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高代次 text_delta 提前推进 current_epoch 后，对账拿到同状态新代次快照时仍必须补发。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 0.0)

    class EpochAdvanceRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1, valid_epochs={2})
            self.snapshot_calls = 0

        async def get_stream_snapshot(
            self, session_id: str, owner_id: str, run_id: str
        ) -> RunStreamSnapshot:
            assert self.subscribed
            self.snapshot_calls += 1
            # 首次快照为 RUNNING(epoch=1)；后续对账返回同状态但代次升至 epoch=2 的快照。
            epoch = 1 if self.snapshot_calls == 1 else 2
            return RunStreamSnapshot(_run(RunStatus.RUNNING, epoch), (), ())

    incoming: list[RunStreamEvent] = [
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="epoch-2-first"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="epoch-2-second"),
        ),
    ]
    repository = EpochAdvanceRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        # 1: 初始 snapshot(epoch=1)
        # 2: text_delta(epoch=2, "epoch-2-first")
        # 3: 对账补发的权威 snapshot(epoch=2)
        # 4: text_delta(epoch=2, "epoch-2-second")（验证未重复发 snapshot 且未误报 stream_gap）
        items = [await anext(iterator) for _ in range(4)]
        await iterator.aclose()
        return items

    events = asyncio.run(collect())
    assert isinstance(events[0], RunSnapshotEvent)
    assert events[0].execution_epoch == 1
    assert isinstance(events[1], RunTextDeltaEvent)
    assert events[1].execution_epoch == 2
    assert events[1].payload.text == "epoch-2-first"
    assert isinstance(events[2], RunSnapshotEvent)
    assert events[2].execution_epoch == 2
    assert events[2].payload.status is RunStatus.RUNNING
    assert isinstance(events[3], RunTextDeltaEvent)
    assert events[3].execution_epoch == 2
    assert events[3].payload.text == "epoch-2-second"
