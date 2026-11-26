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
    """发布失败后，恢复发布先发送中断标记，避免静默缺片。"""
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
            self.history: dict[str, list[bytes]] = {}
            self.subscribers: set[FakePubSub] = set()
            self.fail_next_publish = False

        def pubsub(self) -> FakePubSub:
            return FakePubSub(self)

        async def publish(self, channel: str, raw: bytes) -> int:
            if self.fail_next_publish:
                self.fail_next_publish = False
                raise RedisError("publish temporarily unavailable")
            self.history.setdefault(channel, []).append(raw)
            channel_bytes = channel.encode("utf-8")
            for subscriber in tuple(self.subscribers):
                if channel in subscriber.subscribed_channels:
                    subscriber.messages.put_nowait(
                        {"type": "message", "channel": channel_bytes, "data": raw}
                    )
            return len(self.subscribers)

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
        redis.fail_next_publish = True
        with pytest.raises(RunEventTransportError):
            await transport.publish_text(run_id, 1, "lost same epoch part")
        await transport.publish_text(run_id, 1, "hidden same epoch suffix")
        redis.fail_next_publish = True
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


def test_redis_local_sequence_monotonic_eviction_and_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程内序号按 (run_id, execution_epoch) 独立递增、终态淘汰、LRU 限容且校验 64KB 帧上限。"""
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
        # run_id 与 text 内故意包含类似 JSON 序号片段，验证完全基于模型序列化无任何字节切片污染。
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
        assert first.stream_sequence == 1
        assert first.payload.text == tricky_text
        assert first.payload.round == 2
        assert isinstance(second, RunTextDeltaEvent)
        assert second.stream_sequence == 2
        assert isinstance(third, RunTextDeltaEvent)
        assert third.execution_epoch == 8 and third.stream_sequence == 1

        # 子 Run 终态事件转发到父频道时不淘汰父 Run 的序号状态。
        now = datetime.now(UTC)
        child_completed = RunEvent(
            id="evt_child_done",
            run_id="sub_run_1",
            sequence=2,
            type="completed",
            created_at=now,
            payload=RunCompletedPayload(assistant_message_id="exec_sub_1"),
        )
        await transport.publish_persisted(tricky_run_id, 7, child_completed)
        assert (tricky_run_id, 7) in transport._sequences

        # 本 Run 终态事件发布完成后立即清理该代次序号与失败记录。
        parent_completed = RunEvent(
            id="evt_parent_done",
            run_id=tricky_run_id,
            sequence=3,
            type="completed",
            created_at=now,
            payload=RunCompletedPayload(assistant_message_id="msg_final_1"),
        )
        await transport.publish_persisted(tricky_run_id, 7, parent_completed)
        assert (tricky_run_id, 7) not in transport._sequences

        # LRU 容量上限淘汰最旧键，防止常驻 Worker 内存泄漏。
        monkeypatch.setattr(redis_module, "MAX_TRACKED_EPOCHS", 2)
        await transport.publish_text("run_lru_1", 1, "x")
        await transport.publish_text("run_lru_2", 1, "y")
        await transport.publish_text("run_lru_3", 1, "z")
        assert len(transport._sequences) == 2
        assert ("run_lru_1", 1) not in transport._sequences
        assert ("run_lru_2", 1) in transport._sequences
        assert ("run_lru_3", 1) in transport._sequences

        # 超过单帧 64KB 预算时明确报错，不发送到 Redis。
        monkeypatch.setattr(redis_module, "MAX_REDIS_EVENT_BYTES", 32)
        with pytest.raises(ValueError, match="run_stream_event_exceeds_redis_limit"):
            await transport.publish_persisted("run_oversize", 1, parent_completed)

    asyncio.run(scenario())


def test_redis_split_text_chunks_utf8_and_json_escape_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_split_text_chunks 按 UTF-8 安全字节边界与 JSON 转义预算切分，严格遵守 64KB 帧上限。"""
    # 小包与空串快路径。
    assert RedisRunTransport._split_text_chunks("run_empty", 1, "", round=0) == [""]
    small = 'hello 世界🙂\x00"\\' * 50
    assert RedisRunTransport._split_text_chunks("run_fast", 1, small, round=3) == [small]

    # 中包整串一次放行。
    medium = "a" * 20_000
    assert RedisRunTransport._split_text_chunks("run_med", 1, medium, round=0) == [medium]

    # 超长多字节文本按合法 UTF-8 边界切分，无损拼接且每片满足 Redis 与 SSE 双重字节预算。
    large = "界🙂" * 20_000
    chunks = RedisRunTransport._split_text_chunks("run_big", 9, large, round=4)
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
        assert fitted_len <= redis_module.MAX_REDIS_EVENT_BYTES
        assert fitted_len + redis_module.SSE_PREFIX_RESERVE <= redis_module.MAX_SSE_EVENT_BYTES

    # 高密度 JSON 转义字符（控制字符、引号、反斜杠）同样精确收缩至预算内。
    heavy_escape = '"\x00\\\n' * 12_000
    escaped_chunks = RedisRunTransport._split_text_chunks("run_esc", 2, heavy_escape, round=1)
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
        assert fitted_len <= redis_module.MAX_REDIS_EVENT_BYTES
        assert fitted_len + redis_module.SSE_PREFIX_RESERVE <= redis_module.MAX_SSE_EVENT_BYTES

    # 动态读取 monkeypatch 后的极小上限，单字符即超限时明确报错。
    monkeypatch.setattr(redis_module, "MAX_REDIS_EVENT_BYTES", 32)
    with pytest.raises(ValueError, match="run_stream_text_chunk_exceeds_redis_limit"):
        RedisRunTransport._split_text_chunks("run_tiny", 1, "a", round=0)


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
    """终态快照填充自包含消息与 assistant_message_id，兼容旧格式并优先裁事件保护终态消息。"""
    from collections.abc import Sequence
    from types import SimpleNamespace

    from adapter.run.model import RunModel
    from adapter.run.repository import context_events as ctx_module
    from adapter.run.repository.context_events import RunContextEventsMixin

    now = datetime.now(UTC)
    # 1. 验证旧格式 RunStreamMessage / RunSnapshotPayload JSON 100% 向后兼容反序列化。
    legacy_snapshot_json = (
        '{"type":"snapshot","run_id":"r1","execution_epoch":1,"stream_sequence":0,'
        '"payload":{"status":"completed","event_sequence":2,"messages":[{"id":"m1",'
        '"role":"assistant","status":"completed","text":"legacy","sequence":2,'
        '"created_at":"2026-01-01T00:00:00Z"}],"events":[]}}'
    )
    parsed_legacy = redis_module._EVENT_ADAPTER.validate_json(legacy_snapshot_json)
    assert isinstance(parsed_legacy, RunSnapshotEvent)
    assert parsed_legacy.payload.assistant_message_id is None
    assert parsed_legacy.payload.messages[0].parts is None
    assert parsed_legacy.payload.messages[0].session_id is None

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
        model_snapshot_json=None,
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
            managed_run_id="run_term_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id=None,
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
            managed_run_id="run_term_1",
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata_json={},
            context_turn_id=None,
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
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    class _Repo(RunContextEventsMixin):
        def __init__(self) -> None:
            self._session_factory = lambda: _FakeSession()  # type: ignore[assignment]

    # 紧凑预算：全量消息+事件超限，裁掉旧用户消息与尾部事件后可完整保留最终助手消息。
    monkeypatch.setattr(ctx_module, "_STREAM_SNAPSHOT_BYTES", 2000)
    snapshot = asyncio.run(_Repo().get_stream_snapshot("ses_term_1", "usr_term_1", "run_term_1"))
    assert snapshot is not None
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
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from adapter.auth.model import UserModel
    from adapter.base import Base
    from adapter.message.model import ConversationMessageModel
    from adapter.message.repository import SqlAlchemyMessageRepository
    from adapter.run.model import RunEventModel, RunModel
    from adapter.run.repository import SqlAlchemyRunRepository
    from adapter.session.model import SessionModel

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )
        now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
        try:
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_sq_1",
                        email="sq@example.test",
                        password_hash="h",
                        status=UserStatus.ACTIVE.value,
                        role=UserRole.MEMBER.value,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    SessionModel(
                        id="ses_sq_1",
                        owner_id="usr_sq_1",
                        title="SQLite 终态快照回归",
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                    )
                )
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
                            managed_run_id="run_sq_1",
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
                            managed_run_id="run_sq_1",
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
