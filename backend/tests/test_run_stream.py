"""Run SSE 服务、权威快照和 Cookie 认证回归。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

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


def test_stream_gap_interrupts_and_suppresses_incomplete_delta_suffix() -> None:
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
                stream_sequence=1,
                payload=RunTextDeltaPayload(text="first"),
            ),
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=1,
                stream_sequence=3,
                payload=RunTextDeltaPayload(text="tail after lost chunk"),
            ),
            RunTextDeltaEvent(
                run_id="run_stream_test",
                execution_epoch=2,
                stream_sequence=1,
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
    assert isinstance(events[1], RunTextDeltaEvent)
    assert events[1].payload.text == "first"
    assert isinstance(events[2], RunInterruptedEvent)
    assert events[2].payload.reason == "stream_gap"
    assert isinstance(events[3], RunTextDeltaEvent)
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


def test_pubsub_disconnect_reconnects_with_interrupt_and_fresh_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pub/Sub 中断后发 interruption，再从数据库重新校准快照。"""
    import app.run.stream as stream_module

    monkeypatch.setattr(stream_module, "REDIS_RECONNECT_SECONDS", 0.0)
    monkeypatch.setattr(stream_module, "DATABASE_RECONCILE_SECONDS", 100.0)

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

    class ReconnectingTransport(_FakeTransport):
        def __init__(self, repository: ChangingRepository) -> None:
            super().__init__(repository)
            self.connections = 0

        @asynccontextmanager
        async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
            self.connections += 1
            self.repository.subscribed = True
            subscription: RunEventSubscription = (
                cast(RunEventSubscription, BrokenSubscription([]))
                if self.connections == 1
                else cast(
                    RunEventSubscription,
                    _FakeSubscription(
                        [
                            RunTextDeltaEvent(
                                run_id="run_stream_test",
                                execution_epoch=1,
                                stream_sequence=2,
                                payload=RunTextDeltaPayload(text="must stay hidden"),
                            )
                        ]
                    ),
                )
            )
            yield subscription

    repository = ChangingRepository()
    transport = ReconnectingTransport(repository)
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
    assert isinstance(events[0], RunSnapshotEvent)
    assert events[1].type == "stream_interrupted"
    assert events[1].payload.reason == "redis_unavailable"
    assert events[2].type == "stream_interrupted"
    assert events[2].payload.reason == "redis_reconnected"
    assert isinstance(events[3], RunSnapshotEvent)
    assert events[3].payload.status is RunStatus.COMPLETED
    assert all(
        not isinstance(event, RunTextDeltaEvent) or event.payload.text != "must stay hidden"
        for event in events
    )


def test_redis_subscription_overflow_surfaces_for_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过有界订阅队列时通知服务断开并重新对账。"""
    monkeypatch.setattr(redis_module, "MAX_SUBSCRIPTION_EVENTS", 1)
    event = RunTextDeltaEvent(
        run_id="run_stream_test",
        execution_epoch=1,
        stream_sequence=1,
        payload=RunTextDeltaPayload(text="delta"),
    )
    channel = RedisRunTransport._channel("run_stream_test").encode("utf-8")

    class FakePubSub:
        def __init__(self) -> None:
            self.messages: deque[dict[str, object]] = deque()
            for _ in range(2):
                self.messages.append(
                    {
                        "type": "message",
                        "channel": channel,
                        "data": event.model_dump_json().encode(),
                    }
                )

        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            if self.messages:
                return self.messages.popleft()
            await asyncio.sleep(0.01)
            return None

        async def aclose(self) -> None:
            return None

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return FakePubSub()

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = RedisRunTransport(cast(Redis, FakeRedis()))
        async with transport.subscribe("run_stream_test") as subscription:
            await asyncio.sleep(0.02)
            with pytest.raises(RunEventTransportError, match="run_stream_buffer_overflow"):
                await subscription.get(timeout=0.1)
        await transport.aclose()

    asyncio.run(scenario())


def test_publisher_failure_before_sequence_is_reported_before_next_delta() -> None:
    """序号分配失败后，恢复发布先发送中断标记，避免静默缺片。"""
    from redis.exceptions import RedisError

    class FailingRedis:
        def __init__(self) -> None:
            self.sequence = 0
            self.fail_next_sequence = True
            self.published: list[bytes] = []

        async def _exec_publish_script(
            self,
            numkeys: int,
            key: str,
            ttl: int,
            prefix: bytes,
            suffix: bytes,
            channel: str,
            max_bytes: int,
        ) -> int:
            del numkeys, key, ttl, channel
            if self.fail_next_sequence:
                self.fail_next_sequence = False
                raise RedisError("sequence unavailable")
            self.sequence += 1
            raw = prefix + str(self.sequence).encode("utf-8") + suffix
            if len(raw) > int(max_bytes):
                return -1
            self.published.append(raw)
            return self.sequence

        async def evalsha(self, sha: str, *args: object) -> int:
            del sha
            return await self._exec_publish_script(*args)  # type: ignore[arg-type]

        async def eval(self, script: str, *args: object) -> int:
            del script
            return await self._exec_publish_script(*args)  # type: ignore[arg-type]

    async def scenario() -> list[RunStreamEvent]:
        client = FailingRedis()
        transport = RedisRunTransport(cast(Redis, client))
        with pytest.raises(RunEventTransportError):
            await transport.publish_text("run_failure_test", 3, "lost delta")
        await transport.publish_text("run_failure_test", 3, "next delta")
        return [redis_module._EVENT_ADAPTER.validate_json(raw) for raw in client.published]

    events = asyncio.run(scenario())
    assert isinstance(events[0], RunInterruptedEvent)
    assert events[0].payload.reason == "publisher_error"
    assert isinstance(events[1], RunTextDeltaEvent)
    assert events[1].payload.text == "next delta"


def test_publisher_to_public_sse_suppresses_failed_epoch_suffixes() -> None:
    """真实 transport 的发布中断经服务送到 SSE，失败代次尾片不再展示。"""
    from redis.exceptions import RedisError

    run_id = "run_full_stream_test"
    session_id = "session_full_stream_test"

    class FakePubSub:
        def __init__(self, redis: FakeRedis) -> None:
            self.redis = redis
            self.subscribed_channels: set[str] = set()
            self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, channel_name: str) -> None:
            self.subscribed_channels.add(channel_name)
            self.redis.subscribers.add(self)
            channel_bytes = channel_name.encode("utf-8")
            for raw in self.redis.history.get(channel_name, []):
                self.messages.put_nowait({"type": "message", "channel": channel_bytes, "data": raw})

        async def unsubscribe(self, channel_name: str) -> None:
            self.subscribed_channels.discard(channel_name)

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            try:
                return await asyncio.wait_for(self.messages.get(), timeout=1.0)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            self.subscribed_channels.clear()
            self.redis.subscribers.discard(self)

    class FakeRedis:
        def __init__(self) -> None:
            self.counters: dict[str, int] = {}
            self.history: dict[str, list[bytes]] = {}
            self.subscribers: set[FakePubSub] = set()
            self.fail_next_sequence = False

        def pubsub(self) -> FakePubSub:
            return FakePubSub(self)

        async def _exec_publish_script(
            self,
            numkeys: int,
            key: str,
            ttl: int,
            prefix: bytes,
            suffix: bytes,
            channel: str,
            max_bytes: int,
        ) -> int:
            del numkeys, ttl
            if self.fail_next_sequence:
                self.fail_next_sequence = False
                raise RedisError("sequence temporarily unavailable")
            self.counters[key] = self.counters.get(key, 0) + 1
            sequence = self.counters[key]
            raw = prefix + str(sequence).encode("utf-8") + suffix
            if len(raw) > int(max_bytes):
                return -1
            self.history.setdefault(channel, []).append(raw)
            channel_bytes = channel.encode("utf-8")
            for subscriber in tuple(self.subscribers):
                if channel in subscriber.subscribed_channels:
                    subscriber.messages.put_nowait(
                        {"type": "message", "channel": channel_bytes, "data": raw}
                    )
            return sequence

        async def evalsha(self, sha: str, *args: object) -> int:
            del sha
            return await self._exec_publish_script(*args)  # type: ignore[arg-type]

        async def eval(self, script: str, *args: object) -> int:
            del script
            return await self._exec_publish_script(*args)  # type: ignore[arg-type]

        async def aclose(self) -> None:
            return None

    class TerminalRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1, valid_epochs={1, 2, 3})
            self.snapshots = 0

        async def get(self, session: str, owner: str, run: str) -> Run:
            return replace(_run(self.status, self.epoch), id=run, session_id=session)

        async def get_stream_snapshot(
            self, session: str, owner: str, run: str
        ) -> RunStreamSnapshot:
            self.subscribed = True
            self.snapshots += 1
            status = RunStatus.RUNNING if self.snapshots == 1 else RunStatus.COMPLETED
            epoch = 1 if self.snapshots == 1 else 3
            return RunStreamSnapshot(
                replace(_run(status, epoch), id=run, session_id=session), (), ()
            )

    redis = FakeRedis()
    transport = RedisRunTransport(cast(Redis, redis))

    async def publish_scenario() -> None:
        await transport.publish_text(run_id, 1, "visible prefix")
        redis.fail_next_sequence = True
        with pytest.raises(RunEventTransportError):
            await transport.publish_text(run_id, 1, "lost same epoch part")
        await transport.publish_text(run_id, 1, "hidden same epoch suffix")
        redis.fail_next_sequence = True
        with pytest.raises(RunEventTransportError):
            await transport.publish_text(run_id, 2, "lost first new epoch part")
        await transport.publish_text(run_id, 2, "hidden new epoch suffix")
        await transport.publish_text(run_id, 3, "visible fresh epoch")
        event = RunEvent(
            id="event_finish_stream",
            run_id=run_id,
            sequence=2,
            type="queued",
            created_at=datetime.now(UTC),
            payload=RunQueuedPayload(trigger_message_id="trigger"),
        )
        await transport.publish_persisted(run_id, 3, event)

    asyncio.run(publish_scenario())
    repository = TerminalRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, transport),
    )
    app = FastAPI()
    app.state.authenticator = _FakeAuthenticator()
    app.include_router(build_run_router(cast(RunService, object()), service))
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
            cookies={"athena_session": "valid-token"},
        )
    assert response.status_code == 200
    assert "visible prefix" in response.text
    assert '"reason":"publisher_error"' in response.text
    assert "hidden same epoch suffix" not in response.text
    assert "hidden new epoch suffix" not in response.text
    assert "visible fresh epoch" in response.text
    assert 'status":"completed' in response.text


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


def test_redis_pump_translates_client_errors_to_transport_error() -> None:
    """订阅泵内的 Redis 客户端异常翻译为领域传输异常、保留原因并立即唤醒消费者。"""
    from redis.exceptions import RedisError

    class FailingPubSub:
        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            raise RedisError("pump failed")

        async def aclose(self) -> None:
            return None

    class FailingRedis:
        def pubsub(self) -> FailingPubSub:
            return FailingPubSub()

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = RedisRunTransport(cast(Redis, FailingRedis()))
        async with transport.subscribe("run_pump_fail") as subscription:
            pump = transport._pump_task
            assert pump is not None
            await asyncio.wait_for(pump, timeout=1.0)
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


def test_stream_caches_live_execution_within_ttl_and_invalidates_on_interrupt_or_epoch() -> None:
    """短 TTL 内同代次增量复用存活校验；中断或代次切换立即重新查库。"""
    from domain.run.model import RunInterruptedPayload

    for invalid_ttl in (-0.1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="run_stream_live_check_ttl_invalid"):
            RunStreamService(
                cast(RunRepository, _FakeRepository()),
                cast(Authenticator, _FakeAuthenticator()),
                None,
                live_check_ttl_seconds=invalid_ttl,
            )

    class CountingRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__(status=RunStatus.RUNNING, epoch=1, valid_epochs={1, 2})
            self.live_checks: list[int] = []

        async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
            del run_id
            self.live_checks.append(execution_epoch)
            return execution_epoch in (self.valid_epochs or set())

    incoming: list[RunStreamEvent] = [
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="a"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="b"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=3,
            payload=RunTextDeltaPayload(text="c"),
        ),
        RunInterruptedEvent(
            run_id="run_stream_test",
            execution_epoch=1,
            stream_sequence=4,
            payload=RunInterruptedPayload(reason="publisher_error"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="d"),
        ),
        RunTextDeltaEvent(
            run_id="run_stream_test",
            execution_epoch=2,
            stream_sequence=2,
            payload=RunTextDeltaPayload(text="e"),
        ),
    ]
    repository = CountingRepository()
    service = RunStreamService(
        cast(RunRepository, repository),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(repository, incoming)),
        live_check_ttl_seconds=1.0,
    )

    async def collect() -> list[RunStreamEvent]:
        iterator = service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        # 1 snapshot + 3 deltas(epoch=1) + 1 interrupted(epoch=1) + 2 deltas(epoch=2) = 7
        items = [await anext(iterator) for _ in range(7)]
        await iterator.aclose()
        return items

    events = asyncio.run(collect())
    assert [event.payload.text for event in events if isinstance(event, RunTextDeltaEvent)] == [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]
    # epoch=1 连续 3 条增量只查库 1 次；收到中断强制复核 1 次；切到 epoch=2 后 2 条增量只查库 1 次。
    assert repository.live_checks == [1, 1, 2]

    # TTL=0 时禁用缓存，每条增量都实时查库。
    no_cache_repo = CountingRepository()
    no_cache_service = RunStreamService(
        cast(RunRepository, no_cache_repo),
        cast(Authenticator, _FakeAuthenticator()),
        cast(RunEventTransport, _FakeTransport(no_cache_repo, incoming[:3])),
        live_check_ttl_seconds=0.0,
    )

    async def collect_no_cache() -> None:
        iterator = no_cache_service.events(
            "session_stream_test", "user_stream_test", "run_stream_test", "token"
        )
        for _ in range(4):
            await anext(iterator)
        await iterator.aclose()

    asyncio.run(collect_no_cache())
    assert no_cache_repo.live_checks == [1, 1, 1]


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
        # 1 初始 snapshot + 5 条补齐的 persisted events (seq 2..6) + 1 条 stream_gap 中断事件 = 7
        emitted = [await anext(iterator) for _ in range(7)]
        await iterator.aclose()
        return emitted

    emitted = asyncio.run(collect())
    assert isinstance(emitted[0], RunSnapshotEvent)
    assert [
        item.payload.event.sequence for item in emitted[1:6] if isinstance(item, RunPersistedEvent)
    ] == [2, 3, 4, 5, 6]
    assert isinstance(emitted[6], RunInterruptedEvent)
    assert emitted[6].payload.reason == "stream_gap"
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


def test_redis_publish_falls_back_from_noscript_evalsha_to_eval() -> None:
    """EVALSHA 遇到 NoScriptError 时自动回退 EVAL，不误报传输故障且正确替换顶层序号。"""
    from redis.exceptions import NoScriptError

    class NoScriptFallbackRedis:
        def __init__(self) -> None:
            self.loaded_sha: str | None = None
            self.evalsha_calls = 0
            self.eval_calls = 0
            self.sequence = 0
            self.published: list[tuple[str, bytes]] = []
            self.force_limit_error = False

        async def evalsha(
            self,
            sha: str,
            numkeys: int,
            key: str,
            ttl: int,
            prefix: bytes,
            suffix: bytes,
            channel: str,
            max_bytes: int,
        ) -> int:
            del numkeys, key, ttl
            self.evalsha_calls += 1
            if self.loaded_sha != sha:
                raise NoScriptError("NOSCRIPT No matching script.")
            return self._finish(prefix, suffix, channel, max_bytes)

        async def eval(
            self,
            script: str,
            numkeys: int,
            key: str,
            ttl: int,
            prefix: bytes,
            suffix: bytes,
            channel: str,
            max_bytes: int,
        ) -> int:
            del numkeys, key, ttl
            self.eval_calls += 1
            assert script == redis_module._PUBLISH_LUA_SCRIPT
            self.loaded_sha = redis_module._PUBLISH_SCRIPT_SHA
            return self._finish(prefix, suffix, channel, max_bytes)

        def _finish(self, prefix: bytes, suffix: bytes, channel: str, max_bytes: int) -> int:
            if self.force_limit_error:
                return -1
            self.sequence += 1
            raw = prefix + str(self.sequence).encode("utf-8") + suffix
            if len(raw) > int(max_bytes):
                return -1
            self.published.append((channel, raw))
            return self.sequence

    async def scenario() -> None:
        client = NoScriptFallbackRedis()
        transport = RedisRunTransport(cast(Redis, client))
        # run_id 与 text 内故意包含与顶层占位符相似的文本，验证只替换真正的顶层 stream_sequence。
        tricky_run_id = 'run_"stream_sequence":0'
        tricky_text = ',"stream_sequence":0,"payload":{"fake":1}'
        await transport.publish_text(tricky_run_id, 7, tricky_text, round=2)
        await transport.publish_text(tricky_run_id, 7, "second chunk", round=2)

        assert client.evalsha_calls == 2
        assert client.eval_calls == 1
        assert len(client.published) == 2

        first = redis_module._EVENT_ADAPTER.validate_json(client.published[0][1])
        second = redis_module._EVENT_ADAPTER.validate_json(client.published[1][1])
        assert isinstance(first, RunTextDeltaEvent)
        assert first.run_id == tricky_run_id
        assert first.stream_sequence == 1
        assert first.payload.text == tricky_text
        assert first.payload.round == 2
        assert isinstance(second, RunTextDeltaEvent)
        assert second.stream_sequence == 2
        assert second.payload.text == "second chunk"

        # Lua 返回 -1 时按超限错误处理，不吞掉错误。
        client.force_limit_error = True
        with pytest.raises(ValueError, match="run_stream_event_exceeds_redis_limit"):
            await transport.publish_text(tricky_run_id, 7, "overflow")

    asyncio.run(scenario())


def test_redis_max_text_chunk_length_fast_paths_and_binary_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_max_text_chunk_length 覆盖 O(1) 小包快路径、中包整串快路径与超长包二分回退。"""
    # 小包命中 O(1) 上界快路径：ASCII、多字节 UTF-8、控制字符与转义引号均可完整放行。
    small = 'hello 世界🙂\x00"\\' * 50
    assert RedisRunTransport._max_text_chunk_length("run_fast", 1, small, round=3) == len(small)

    # 中包超出 len*6 上界但实际 UTF-8 JSON 仍在预算内，命中二级整串快路径。
    medium = "a" * 20_000
    assert RedisRunTransport._max_text_chunk_length("run_med", 1, medium, round=0) == len(medium)

    # 超长多字节文本触发二分切分，且每片满足 Redis 与 SSE 双重字节预算。
    large = "界🙂" * 20_000
    chunk_len = RedisRunTransport._max_text_chunk_length("run_big", 9, large, round=4)
    assert 0 < chunk_len < len(large)
    fitted = RunTextDeltaEvent(
        run_id="run_big",
        execution_epoch=9,
        stream_sequence=9_999_999_999_999_999_999,
        payload=RunTextDeltaPayload(text=large[:chunk_len], round=4),
    )
    fitted_len = len(fitted.model_dump_json(exclude_none=True).encode("utf-8"))
    assert fitted_len <= redis_module.MAX_REDIS_EVENT_BYTES
    assert fitted_len + redis_module.SSE_PREFIX_RESERVE <= redis_module.MAX_SSE_EVENT_BYTES

    next_one = RunTextDeltaEvent(
        run_id="run_big",
        execution_epoch=9,
        stream_sequence=9_999_999_999_999_999_999,
        payload=RunTextDeltaPayload(text=large[: chunk_len + 1], round=4),
    )
    next_len = len(next_one.model_dump_json(exclude_none=True).encode("utf-8"))
    assert (
        next_len > redis_module.MAX_REDIS_EVENT_BYTES
        or next_len + redis_module.SSE_PREFIX_RESERVE > redis_module.MAX_SSE_EVENT_BYTES
    )

    # 动态读取 monkeypatch 后的极小上限，单字符即超限时明确报错。
    monkeypatch.setattr(redis_module, "MAX_REDIS_EVENT_BYTES", 32)
    with pytest.raises(ValueError, match="run_stream_text_chunk_exceeds_redis_limit"):
        RedisRunTransport._max_text_chunk_length("run_tiny", 1, "a", round=0)


def test_redis_multiplexer_shares_single_pubsub_and_fans_out_with_single_deserialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 run_id 多订阅者复用单条 Pub/Sub 连接且单次反序列化扇出；跨频道按引用计数退订。"""

    class SharedPubSub:
        def __init__(self) -> None:
            self.subscribed: list[str] = []
            self.unsubscribed: list[str] = []
            self.closed = False
            self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, channel_name: str) -> None:
            self.subscribed.append(channel_name)

        async def unsubscribe(self, channel_name: str) -> None:
            self.unsubscribed.append(channel_name)

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            try:
                return await asyncio.wait_for(self.queue.get(), timeout=0.05)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            self.closed = True

    class SharedRedis:
        def __init__(self) -> None:
            self.pubsubs: list[SharedPubSub] = []

        def pubsub(self) -> SharedPubSub:
            ps = SharedPubSub()
            self.pubsubs.append(ps)
            return ps

        async def aclose(self) -> None:
            return None

    validate_calls = 0
    original_adapter = redis_module._EVENT_ADAPTER

    class CountingAdapter:
        def validate_json(self, raw: bytes) -> RunStreamEvent:
            nonlocal validate_calls
            validate_calls += 1
            return cast(RunStreamEvent, original_adapter.validate_json(raw))

    monkeypatch.setattr(redis_module, "_EVENT_ADAPTER", CountingAdapter())

    async def scenario() -> None:
        client = SharedRedis()
        transport = RedisRunTransport(cast(Redis, client))
        chan_a = RedisRunTransport._channel("run_a")
        chan_b = RedisRunTransport._channel("run_b")
        ev_a = RunTextDeltaEvent(
            run_id="run_a",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="fanout-a"),
        )
        ev_b = RunTextDeltaEvent(
            run_id="run_b",
            execution_epoch=1,
            stream_sequence=1,
            payload=RunTextDeltaPayload(text="only-b"),
        )

        async with transport.subscribe("run_a") as sub_a1:
            async with transport.subscribe("run_a") as sub_a2:
                async with transport.subscribe("run_b") as sub_b:
                    assert len(client.pubsubs) == 1
                    ps = client.pubsubs[0]
                    # 同频道第二个订阅者零 Redis subscribe 往返；不同频道各 subscribe 一次。
                    assert ps.subscribed == [chan_a, chan_b]

                    ps.queue.put_nowait(
                        {
                            "type": "message",
                            "channel": chan_a.encode("utf-8"),
                            "data": ev_a.model_dump_json().encode("utf-8"),
                        }
                    )
                    got_a1 = await sub_a1.get(timeout=1.0)
                    got_a2 = await sub_a2.get(timeout=1.0)
                    assert got_a1 == ev_a
                    assert got_a2 == ev_a
                    # 单次反序列化扇出给两个订阅者。
                    assert validate_calls == 1
                    assert await sub_b.get(timeout=0.05) is None

                # sub_b 退出后，run_b 频道引用归零且 run_a 仍活跃：精确调用一次 unsubscribe。
                assert ps.unsubscribed == [chan_b]
                assert not ps.closed

            # sub_a2 退出但 sub_a1 仍在：不退订 chan_a。
            assert ps.unsubscribed == [chan_b]
            assert not ps.closed

            ps.queue.put_nowait(
                {
                    "type": "message",
                    "channel": chan_a.encode("utf-8"),
                    "data": ev_a.model_dump_json().encode("utf-8"),
                }
            )
            assert await sub_a1.get(timeout=1.0) == ev_a

            # 已退订频道的迟到消息被忽略。
            ps.queue.put_nowait(
                {
                    "type": "message",
                    "channel": chan_b.encode("utf-8"),
                    "data": ev_b.model_dump_json().encode("utf-8"),
                }
            )
            assert await sub_a1.get(timeout=0.05) is None

        # 全局订阅数归零：自动停止后台泵并关闭共享 PubSub，空载零连接占用。
        assert client.pubsubs[0].closed is True
        assert transport._pubsub is None
        assert transport._pump_task is None

    asyncio.run(scenario())


def test_redis_multiplexer_isolates_slow_subscriber_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同频道慢客户端队列溢出仅使其自身报错，不影响快客户端持续接收消息。"""
    monkeypatch.setattr(redis_module, "MAX_SUBSCRIPTION_EVENTS", 1)

    class SharedPubSub:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            try:
                return await asyncio.wait_for(self.queue.get(), timeout=0.05)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            return None

    class SharedRedis:
        def __init__(self) -> None:
            self.ps = SharedPubSub()

        def pubsub(self) -> SharedPubSub:
            return self.ps

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = SharedRedis()
        transport = RedisRunTransport(cast(Redis, client))
        chan = RedisRunTransport._channel("run_iso").encode("utf-8")

        def make_msg(seq: int) -> dict[str, object]:
            ev = RunTextDeltaEvent(
                run_id="run_iso",
                execution_epoch=1,
                stream_sequence=seq,
                payload=RunTextDeltaPayload(text=f"m{seq}"),
            )
            return {
                "type": "message",
                "channel": chan,
                "data": ev.model_dump_json().encode("utf-8"),
            }

        async with transport.subscribe("run_iso") as slow_sub:
            async with transport.subscribe("run_iso") as fast_sub:
                client.ps.queue.put_nowait(make_msg(1))
                first = await fast_sub.get(timeout=1.0)
                assert isinstance(first, RunTextDeltaEvent) and first.payload.text == "m1"

                # 第 2 条消息到达时 slow_sub 队列已满（上限 1），slow_sub 溢出但 fast_sub 正常接收。
                client.ps.queue.put_nowait(make_msg(2))
                second = await fast_sub.get(timeout=1.0)
                assert isinstance(second, RunTextDeltaEvent) and second.payload.text == "m2"

                with pytest.raises(RunEventTransportError, match="run_stream_buffer_overflow"):
                    await slow_sub.get(timeout=0.1)

                # slow_sub 溢出后 fast_sub 依然能持续正常消费。
                client.ps.queue.put_nowait(make_msg(3))
                third = await fast_sub.get(timeout=1.0)
                assert isinstance(third, RunTextDeltaEvent) and third.payload.text == "m3"

    asyncio.run(scenario())


def test_redis_multiplexer_broadcasts_shared_disconnect_and_wakes_all_subscribers() -> None:
    """共享 Pub/Sub 断连故障广播至全部活跃订阅者，立即唤醒阻塞的 get() 且支持后续重建。"""
    from redis.exceptions import RedisError

    class FlakyPubSub:
        def __init__(self, *, fail_gate: asyncio.Event | None) -> None:
            self.fail_gate = fail_gate
            self.closed = False
            self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            if self.fail_gate is not None:
                await self.fail_gate.wait()
                raise RedisError("connection dropped")
            try:
                return await asyncio.wait_for(self.queue.get(), timeout=0.05)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            self.closed = True

    class FlakyRedis:
        def __init__(self) -> None:
            self.fail_gate = asyncio.Event()
            self.pubsubs: list[FlakyPubSub] = []

        def pubsub(self) -> FlakyPubSub:
            ps = FlakyPubSub(fail_gate=self.fail_gate if not self.pubsubs else None)
            self.pubsubs.append(ps)
            return ps

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = FlakyRedis()
        transport = RedisRunTransport(cast(Redis, client))
        async with transport.subscribe("run_d1") as sub1:
            async with transport.subscribe("run_d2") as sub2:
                # 两个订阅者均阻塞在较长超时（5s）的 get() 上。
                waiter1 = asyncio.create_task(sub1.get(timeout=5.0))
                waiter2 = asyncio.create_task(sub2.get(timeout=5.0))
                await asyncio.sleep(0.01)

                client.fail_gate.set()
                for waiter in (waiter1, waiter2):
                    with pytest.raises(
                        RunEventTransportError, match="run_stream_transport_failed"
                    ) as caught:
                        await asyncio.wait_for(waiter, timeout=1.0)
                    assert isinstance(caught.value.__cause__, RedisError)

        assert client.pubsubs[0].closed is True
        assert transport._pubsub is None

        # 故障清池后再次订阅自动新建健康共享 Pub/Sub。
        async with transport.subscribe("run_recovered") as sub3:
            assert len(client.pubsubs) == 2
            ev = RunTextDeltaEvent(
                run_id="run_recovered",
                execution_epoch=1,
                stream_sequence=1,
                payload=RunTextDeltaPayload(text="recovered"),
            )
            client.pubsubs[1].queue.put_nowait(
                {
                    "type": "message",
                    "channel": RedisRunTransport._channel("run_recovered").encode("utf-8"),
                    "data": ev.model_dump_json().encode("utf-8"),
                }
            )
            assert await sub3.get(timeout=1.0) == ev

    asyncio.run(scenario())


def test_redis_multiplexer_trips_shared_pubsub_when_new_channel_subscribe_fails() -> None:
    """共享连接新增频道 subscribe 抛 RedisError 时，熔断旧连接并通知既有订阅者，再向调用方报错。"""
    from redis.exceptions import RedisError

    class HalfBrokenPubSub:
        def __init__(self) -> None:
            self.closed = False
            self.calls = 0

        async def subscribe(self, channel_name: str) -> None:
            del channel_name
            self.calls += 1
            if self.calls == 2:
                raise RedisError("broken pipe on second subscribe")

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            await asyncio.sleep(0.05)
            return None

        async def aclose(self) -> None:
            self.closed = True

    class HalfBrokenRedis:
        def __init__(self) -> None:
            self.ps = HalfBrokenPubSub()

        def pubsub(self) -> HalfBrokenPubSub:
            return self.ps

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = HalfBrokenRedis()
        transport = RedisRunTransport(cast(Redis, client))
        async with transport.subscribe("run_existing") as existing_sub:
            waiter = asyncio.create_task(existing_sub.get(timeout=5.0))
            await asyncio.sleep(0.01)

            with pytest.raises(
                RunEventTransportError, match="run_stream_subscribe_failed"
            ) as caught:
                async with transport.subscribe("run_new"):
                    pass
            assert isinstance(caught.value.__cause__, RedisError)

            with pytest.raises(
                RunEventTransportError, match="run_stream_transport_failed"
            ) as existing_caught:
                await asyncio.wait_for(waiter, timeout=1.0)
            assert isinstance(existing_caught.value.__cause__, RedisError)

        assert client.ps.closed is True
        assert transport._pubsub is None

    asyncio.run(scenario())


def test_redis_transport_aclose_respects_owns_client_while_closing_shared_pubsub() -> None:
    """aclose() 始终关闭共享 Pub/Sub 与后台泵，仅在 owns_client=True 时关闭 Redis 客户端。"""

    class TrackedPubSub:
        def __init__(self) -> None:
            self.closed = False

        async def subscribe(self, channel_name: str) -> None:
            del channel_name

        async def unsubscribe(self, channel_name: str) -> None:
            del channel_name

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            await asyncio.sleep(0.05)
            return None

        async def aclose(self) -> None:
            self.closed = True

    class TrackedRedis:
        def __init__(self) -> None:
            self.client_closed = 0
            self.pubsubs: list[TrackedPubSub] = []

        def pubsub(self) -> TrackedPubSub:
            ps = TrackedPubSub()
            self.pubsubs.append(ps)
            return ps

        async def aclose(self) -> None:
            self.client_closed += 1

    async def scenario() -> None:
        shared_client = TrackedRedis()
        borrowed = RedisRunTransport(cast(Redis, shared_client), owns_client=False)
        async with borrowed.subscribe("run_borrowed"):
            await borrowed.aclose()
            assert shared_client.pubsubs[0].closed is True
            assert borrowed._pubsub is None
            assert borrowed._pump_task is None
            assert shared_client.client_closed == 0

        owned_client = TrackedRedis()
        owned = RedisRunTransport(cast(Redis, owned_client), owns_client=True)
        async with owned.subscribe("run_owned"):
            await owned.aclose()
            assert owned_client.pubsubs[0].closed is True
            assert owned_client.client_closed == 1

    asyncio.run(scenario())


def test_get_stream_snapshot_trims_outside_transaction_and_advances_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_stream_snapshot 在释放事务与连接后再执行消息与 512KB 快照裁剪。"""
    from collections.abc import Sequence
    from types import SimpleNamespace

    from adapter.run.model import RunModel
    from adapter.run.repository import context_events as ctx_module
    from adapter.run.repository.context_events import RunContextEventsMixin
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
    monkeypatch.setattr(ctx_module, "_STREAM_SNAPSHOT_BYTES", 2048)

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
        model_snapshot_json=None,
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
            managed_run_id="run_snap_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id=None,
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
            managed_run_id="run_snap_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id=None,
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

    class _FakeSession:
        bind = None

        def __init__(self) -> None:
            self._scalars_call = 0

        def begin(self) -> _FakeBegin:
            return _FakeBegin()

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

    class _Repo(RunContextEventsMixin):
        def __init__(self) -> None:
            self._session_factory = lambda: _FakeSession()  # type: ignore[assignment]

    snapshot = asyncio.run(_Repo().get_stream_snapshot("ses_snap_1", "usr_snap_1", "run_snap_1"))
    assert snapshot is not None
    # 步骤 7 序列化全部发生在事务与会话完全退出之后。
    assert tx_active_during_snapshot_dump
    assert all(not active for active in tx_active_during_snapshot_dump)
    assert all(not active for active in session_active_during_snapshot_dump)
    # 较旧的超长消息 msg_2 被裁掉，保留 msg_3 且游标推进至 2 + 1 = 3。
    assert [message.id for message in snapshot.messages] == ["msg_3"]
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
