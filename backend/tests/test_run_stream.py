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
from redis.asyncio.client import PubSub
from starlette.types import Message, Send
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
)

from adapter.run import redis_transport as redis_module
from adapter.run.redis_transport import RedisRunTransport, _RedisRunSubscription
from api.runs import _RunEventSourceResponse, build_run_router
from app.run.service import RunService
from app.run.stream import RunStreamService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.auth.ports import Authenticator
from domain.run.model import (
    Run,
    RunCompressionCompletedPayload,
    RunEvent,
    RunInterruptedEvent,
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
            role=UserRole.USER,
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

    class FakePubSub:
        def __init__(self) -> None:
            self.messages: deque[dict[str, object]] = deque()
            for _ in range(2):
                self.messages.append(
                    {
                        "type": "message",
                        "data": event.model_dump_json().encode(),
                    }
                )

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            if self.messages:
                return self.messages.popleft()
            await asyncio.sleep(0.01)
            return None

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        subscription = _RedisRunSubscription(cast(PubSub, FakePubSub()))
        subscription.start()
        await asyncio.sleep(0)
        with pytest.raises(RunEventTransportError, match="run_stream_buffer_overflow"):
            await subscription.get(timeout=0.1)
        await subscription.aclose()

    asyncio.run(scenario())


def test_publisher_failure_before_sequence_is_reported_before_next_delta() -> None:
    """序号分配失败后，恢复发布先发送中断标记，避免静默缺片。"""
    from redis.exceptions import RedisError

    class FailingRedis:
        def __init__(self) -> None:
            self.sequence = 0
            self.fail_next_sequence = True
            self.published: list[bytes] = []

        async def eval(self, *args: object) -> int:
            if self.fail_next_sequence:
                self.fail_next_sequence = False
                raise RedisError("sequence unavailable")
            self.sequence += 1
            return self.sequence

        async def publish(self, channel: str, data: bytes) -> int:
            self.published.append(data)
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
        subscribed_channel: str | None

        def __init__(self, redis: FakeRedis) -> None:
            self.redis = redis
            self.subscribed_channel = None
            self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, channel_name: str) -> None:
            self.subscribed_channel = channel_name
            self.redis.subscribers.add(self)
            for raw in self.redis.history.get(channel_name, []):
                self.messages.put_nowait({"type": "message", "data": raw})

        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            try:
                return await asyncio.wait_for(self.messages.get(), timeout=1.0)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            self.redis.subscribers.discard(self)

    class FakeRedis:
        def __init__(self) -> None:
            self.counters: dict[str, int] = {}
            self.history: dict[str, list[bytes]] = {}
            self.subscribers: set[FakePubSub] = set()
            self.fail_next_sequence = False

        def pubsub(self) -> FakePubSub:
            return FakePubSub(self)

        async def eval(self, script: str, key_count: int, key: str) -> int:
            if self.fail_next_sequence:
                self.fail_next_sequence = False
                raise RedisError("sequence temporarily unavailable")
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key]

        async def publish(self, channel: str, raw: bytes) -> int:
            self.history.setdefault(channel, []).append(raw)
            for subscriber in tuple(self.subscribers):
                if subscriber.subscribed_channel == channel:
                    subscriber.messages.put_nowait({"type": "message", "data": raw})
            return 1

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
    """订阅泵内的 Redis 客户端异常翻译为领域传输异常并保留原因。"""
    from redis.exceptions import RedisError

    class FailingPubSub:
        async def get_message(self, **kwargs: object) -> dict[str, object] | None:
            del kwargs
            raise RedisError("pump failed")

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        subscription = _RedisRunSubscription(cast(PubSub, FailingPubSub()))
        subscription.start()
        pump = subscription._pump_task
        assert pump is not None
        await asyncio.wait_for(pump, timeout=1.0)
        with pytest.raises(RunEventTransportError, match="run_stream_transport_failed") as caught:
            await subscription.get(timeout=0.1)
        assert isinstance(caught.value.__cause__, RedisError)
        await subscription.aclose()

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
