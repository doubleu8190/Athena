"""公开 HTTP、真实 Worker、PostgreSQL 与 Redis/SSE 的上下文安全停机闭环。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from pydantic import TypeAdapter
from redis.asyncio import Redis
from sqlalchemy import event as db_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from adapter.auth.model import UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunEventModel, RunModel, RunToolCallModel
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from api.auth import AuthCookieConfig, build_auth_router
from api.runs import build_run_router
from api.sessions import build_session_router
from app.auth import AuthService
from app.run.config import RunWorkerConfig
from app.run.context import PROTOCOL_TOKEN_RESERVE, count_plain_text_tokens_async
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.run.worker import RunWorker
from app.session.service import SessionService
from domain.llm.model import (
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.run.model import (
    ContextStopReason,
    FailRunCommand,
    Run,
    RunEvent,
    RunFailedPayload,
    RunPersistedEvent,
    RunSnapshotEvent,
    RunStatus,
    RunStreamEvent,
)
from infrastructure.security import Argon2PasswordHasher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
REDIS_URL = os.getenv("ATHENA_REDIS_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL or not REDIS_URL,
        reason="ATHENA_TEST_DATABASE_URL and ATHENA_REDIS_URL are required",
    ),
]

_STREAM_EVENT_ADAPTER: TypeAdapter[RunStreamEvent] = TypeAdapter(cast(Any, RunStreamEvent))


@asynccontextmanager
async def _database(
    database_url: str,
) -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], AsyncEngine, AsyncEngine, str],
    None,
]:
    """创建仅属于当前用例的随机 schema，并把迁移和业务连接固定到该 schema。"""
    async with isolated_postgres_schema(database_url, prefix="athena_v2_context_safety_e2e") as db:
        yield db.session_factory, db.engine, db.admin_engine, db.schema


async def _seed_user_and_profile(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    email: str,
    password_hash: str,
) -> None:
    """写入用于真实登录与 Run 执行的账号和确定性主模型配置。"""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add(
            UserModel(
                id=user_id,
                email=email,
                password_hash=password_hash,
                display_name=None,
                status="active",
                role="member",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            ModelProfileVersionModel(
                id="llmprof_context_safety_e2e",
                name="Context safety E2E deterministic model",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="context-safety-main",
                **encrypted_llm_key_fields("llmprof_context_safety_e2e"),
                context_window_tokens=4096,
                tokenizer_encoding="o200k_base",
                supports_stream=True,
                supports_tools=True,
                max_output_tokens=128,
                request_timeout_seconds=30,
                execution_timeout_seconds=60,
                max_concurrency=1,
                temperature=None,
                enabled=True,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            DefaultModelProfileModel(
                singleton_id=1,
                profile_id="llmprof_context_safety_e2e",
            )
        )


async def _seed_oversize_history(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    history_marker: str,
) -> None:
    """写入仅用于推高业务输入的旧历史；报告投影必须排除这段内容。"""
    now = datetime.now(UTC)
    history_text = f"{history_marker}\n" + (
        "Historical garden maintenance notes unrelated to this Run. " * 1400
    )
    async with session_factory() as session, session.begin():
        session.add(
            ConversationMessageModel(
                id="msg_context_safety_history",
                session_id=session_id,
                role="user",
                status="completed",
                sequence=1,
                parts=[{"type": "text", "text": history_text}],
                reply_to_message_id=None,
                run_id=None,
                managed_run_id=None,
                context_turn_id=None,
                client_message_id=None,
                created_at=now,
                completed_at=now,
                metadata_json={},
            )
        )
        session_row = await session.get(SessionModel, session_id)
        assert session_row is not None
        session_row.last_message_sequence = 1
        session_row.updated_at = now


def _parse_sse(body: str) -> list[RunStreamEvent]:
    """解析响应中的完整 SSE 帧，并用领域模型校验每个事件。"""
    events: list[RunStreamEvent] = []
    for block in body.strip().split("\n\n"):
        event_name: str | None = None
        stream_sequence: int | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("id:"):
                stream_sequence = int(line.removeprefix("id:").strip())
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        if event_name is None or stream_sequence is None or not data_lines:
            continue
        event = _STREAM_EVENT_ADAPTER.validate_json("\n".join(data_lines))
        assert event.type == event_name
        assert event.stream_sequence == stream_sequence
        events.append(event)
    return events


def _failed_snapshot(events: Sequence[RunStreamEvent]) -> RunSnapshotEvent:
    """提取唯一的失败终态快照，避免把较早的 queued 快照误当最终投影。"""
    snapshots = [
        event
        for event in events
        if isinstance(event, RunSnapshotEvent) and event.payload.status is RunStatus.FAILED
    ]
    assert len(snapshots) == 1
    return snapshots[0]


def _assert_authoritative_report_snapshot(
    snapshot: RunSnapshotEvent,
    *,
    run_id: str,
    report_message_id: str,
    context_stop_reason: ContextStopReason,
) -> None:
    """核对失败快照只引用同一份权威报告，并携带稳定的失败原因。"""
    payload = snapshot.payload
    assert snapshot.run_id == run_id
    assert payload.report_message_id == report_message_id
    report_messages = [message for message in payload.messages if message.id == report_message_id]
    assert len(report_messages) == 1
    assert report_messages[0].role == "assistant"
    assert report_messages[0].status == "completed"
    failed_payloads: list[RunFailedPayload] = []
    for event in payload.events:
        if (
            isinstance(event.payload, RunFailedPayload)
            and event.payload.report_message_id == report_message_id
        ):
            failed_payloads.append(event.payload)
    assert len(failed_payloads) == 1
    assert failed_payloads[0].context_stop_reason is context_stop_reason
    assert not any(event.type == "completed" for event in payload.events)


class _SafetyStoppingProvider:
    """记录报告调用并故意失败，确保系统兜底报告仍可原子落库。"""

    def __init__(self) -> None:
        """初始化报告请求与主模型流调用计数。"""
        self.report_requests: list[tuple[RuntimeModelProfile, LlmRequest]] = []
        self.main_stream_calls = 0

    async def complete(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> LlmResponse:
        """记录唯一一次无工具报告请求后模拟供应商失败。"""
        self.report_requests.append((runtime, request))
        raise LlmProviderError("report_provider_failed")

    @asynccontextmanager
    async def stream(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        """业务流一旦被调用即留下证据；返回稳定响应让错误能显式进入断言。"""
        del runtime, request
        self.main_stream_calls += 1

        async def events() -> AsyncIterator[LlmStreamEvent]:
            yield ResponseCompleted(
                LlmResponse(
                    output=(LlmText("unexpected main model output"),),
                    status="completed",
                    incomplete_reason=None,
                    model="context-safety-main",
                    response_id="unexpected-main-response",
                    request_id="unexpected-main-request",
                    usage=LlmUsage(input_tokens=10, output_tokens=4, total_tokens=14),
                )
            )

        yield events()


class _RecordingRunRepository(SqlAlchemyRunRepository):
    """包装真实仓储并保留每次事务失败时的完整 FailRunCommand。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        message_repository: SqlAlchemyMessageRepository,
    ) -> None:
        """复用真实仓储构造，同时初始化失败命令记录。"""
        super().__init__(session_factory, message_repository)
        self.fail_commands: list[FailRunCommand] = []

    async def fail(self, command: FailRunCommand) -> Run | None:
        """先记录再执行原仓储，确保受控事务故障和重放都能被观察。"""
        self.fail_commands.append(command)
        return await super().fail(command)


def _install_report_event_write_fault(
    engine: AsyncEngine,
) -> tuple[Callable[..., None], list[int]]:
    """在 failed 事件 INSERT 前制造一次真实事务错误，事务内报告已先 flush。"""
    failures = [0]

    def before_cursor_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """只命中安全码与报告 ID 同存的 run_events 插入，且整个测试只命中一次。"""
        del connection, cursor, context, executemany
        normalized_statement = " ".join(statement.upper().split())
        parameter_text = str(parameters)
        if (
            failures[0] == 0
            and normalized_statement.startswith("INSERT ")
            and "RUN_EVENTS" in normalized_statement
            and "run_context_safety_stop" in parameter_text
            and "report_message_id" in parameter_text
        ):
            failures[0] += 1
            raise RuntimeError("injected_context_report_event_write_failure")

    db_event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    return before_cursor_execute, failures


@pytest.mark.parametrize(
    "inject_transaction_failure",
    [False, True],
    ids=["normal", "report-event-write-retry"],
)
def test_context_safety_public_run_worker_pg_redis_sse_recovery(
    monkeypatch: pytest.MonkeyPatch,
    inject_transaction_failure: bool,
) -> None:
    """安全报告事务一次失败后须稳定重放，并由 Redis/SSE 继续恢复同一终态。"""
    database_url = DATABASE_URL
    redis_url = REDIS_URL
    if database_url is None or redis_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL and ATHENA_REDIS_URL are required")

    async def scenario() -> None:
        async with _database(database_url) as (
            session_factory,
            engine,
            _admin_engine,
            _schema,
        ):
            hasher = Argon2PasswordHasher()
            user_id = "usr_context_safety_e2e"
            email = "context-safety-e2e@example.com"
            password = "context-safety-e2e-password"
            await _seed_user_and_profile(
                session_factory,
                user_id=user_id,
                email=email,
                password_hash=hasher.hash_password(password),
            )

            auth_repository = SqlAlchemyAuthRepository(session_factory)
            auth_service = AuthService(auth_repository, hasher)
            session_repository = SqlAlchemySessionRepository(session_factory)
            message_repository = SqlAlchemyMessageRepository(session_factory)
            run_repository = _RecordingRunRepository(session_factory, message_repository)
            run_service = RunService(run_repository)
            api_transport = RedisRunTransport(Redis.from_url(redis_url, decode_responses=False))
            worker_transport = RedisRunTransport(Redis.from_url(redis_url, decode_responses=False))
            redis_client = Redis.from_url(
                redis_url,
                decode_responses=False,
                socket_connect_timeout=1,
            )
            app = FastAPI()
            app.state.authenticator = auth_service
            app.state.auth_cookie_name = "athena_session"
            app.state.public_origin = None
            app.include_router(
                build_auth_router(
                    auth_service,
                    cookie=AuthCookieConfig(name="athena_session", secure=False),
                )
            )
            app.include_router(build_session_router(SessionService(session_repository)))
            app.include_router(
                build_run_router(
                    run_service,
                    RunStreamService(run_repository, auth_service, api_transport),
                )
            )

            worker_task: asyncio.Task[None] | None = None
            worker: RunWorker | None = None
            transaction_fault_hook: Callable[..., None] | None = None
            injected_failures: list[int] = []
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    # 账号与 Session 都通过公开 HTTP 建立，随后才提交真实 Run。
                    login = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "email": email,
                            "password": password,
                            "remember_me": False,
                        },
                    )
                    assert login.status_code == 200, login.text
                    assert login.json()["id"] == user_id
                    assert client.cookies.get("athena_session")

                    created_session = await client.post(
                        "/api/v1/sessions",
                        json={"title": "Context safety E2E", "project_id": None},
                    )
                    assert created_session.status_code == 201, created_session.text
                    session_id = created_session.json()["id"]

                    history_marker = f"HISTORY_ONLY_{token_hex(8)}"
                    await _seed_oversize_history(
                        session_factory,
                        session_id,
                        history_marker=history_marker,
                    )

                    trigger_text = (
                        "Safety trigger: answer briefly only after the context safety gate. "
                        f"TRIGGER_MARKER_{token_hex(8)}"
                    )
                    submitted = await client.post(
                        f"/api/v1/sessions/{session_id}/runs",
                        json={
                            "request_id": f"safety-{token_hex(8)}",
                            "text": trigger_text,
                        },
                    )
                    assert submitted.status_code == 201, submitted.text
                    submitted_payload = submitted.json()
                    run_id = submitted_payload["id"]
                    trigger_message_id = submitted_payload["trigger_message_id"]
                    assert submitted_payload["status"] == "queued"

                    stream_task = asyncio.create_task(
                        client.get(
                            f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
                            timeout=20,
                        )
                    )
                    channel = f"athena:run:events:{run_id}"
                    loop = asyncio.get_running_loop()
                    subscribe_deadline = loop.time() + 5
                    while True:
                        subscriber_count = (await redis_client.pubsub_numsub(channel))[0][1]
                        if subscriber_count:
                            break
                        assert loop.time() < subscribe_deadline, (
                            "SSE did not subscribe to its isolated Run channel"
                        )
                        await asyncio.sleep(0.02)

                    async def drop_persisted_notification(
                        event_run_id: str,
                        execution_epoch: int,
                        event: RunEvent,
                    ) -> None:
                        """丢弃 Worker 的 Redis 通知，数据库事件仍保持已提交。"""
                        del event_run_id, execution_epoch, event

                    # 发布丢失不改变数据库事实，SSE 必须靠对账快照恢复失败终态。
                    monkeypatch.setattr(
                        worker_transport,
                        "publish_persisted",
                        drop_persisted_notification,
                    )
                    if inject_transaction_failure:
                        transaction_fault_hook, injected_failures = (
                            _install_report_event_write_fault(engine)
                        )
                    provider = _SafetyStoppingProvider()
                    executor = RunExecutor(
                        run_repository,
                        SqlAlchemyModelProfileRepository(
                            session_factory,
                            TEST_LLM_CIPHER,
                        ),
                        provider,
                        system_prompts=SqlAlchemySystemPromptRepository(session_factory),
                        publisher=worker_transport,
                    )
                    worker = RunWorker(
                        run_repository,
                        executor,
                        RunWorkerConfig(
                            concurrency=1,
                            lease_seconds=6,
                            heartbeat_seconds=1,
                            execution_seconds=60,
                            cancel_poll_seconds=0.05,
                            queue_poll_seconds=0.05,
                            reclaim_seconds=0.5,
                        ),
                        publisher=worker_transport,
                    )
                    worker_task = asyncio.create_task(worker.run())

                    first_stream = await asyncio.wait_for(stream_task, timeout=15)
                    assert first_stream.status_code == 200, first_stream.text
                    first_events = _parse_sse(first_stream.text)
                    first_snapshot = _failed_snapshot(first_events)
                    report_message_id = first_snapshot.payload.report_message_id
                    assert report_message_id is not None
                    _assert_authoritative_report_snapshot(
                        first_snapshot,
                        run_id=run_id,
                        report_message_id=report_message_id,
                        context_stop_reason=ContextStopReason.HARD_WINDOW_EXCEEDED,
                    )

                    top_level_failed_events = [
                        event
                        for event in first_events
                        if isinstance(event, RunPersistedEvent)
                        and event.payload.event.type == "failed"
                    ]
                    assert len(top_level_failed_events) == 1

                    # 以已消费游标重新连接；首帧中断后只从 PostgreSQL 取同一终态。
                    last_stream_sequence = max(event.stream_sequence for event in first_events)
                    reconnected = await client.get(
                        f"/api/v1/sessions/{session_id}/runs/{run_id}/stream",
                        headers={"Last-Event-ID": str(last_stream_sequence)},
                        timeout=10,
                    )
                    assert reconnected.status_code == 200, reconnected.text
                    reconnected_events = _parse_sse(reconnected.text)
                    reconnected_snapshot = _failed_snapshot(reconnected_events)
                    _assert_authoritative_report_snapshot(
                        reconnected_snapshot,
                        run_id=run_id,
                        report_message_id=report_message_id,
                        context_stop_reason=ContextStopReason.HARD_WINDOW_EXCEEDED,
                    )
                    assert not any(
                        isinstance(event, RunPersistedEvent)
                        and event.payload.event.type == "failed"
                        for event in reconnected_events
                    )

                    details = await client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
                    assert details.status_code == 200, details.text
                    assert details.json()["status"] == "failed"
                    assert details.json()["safe_error_code"] == "run_context_safety_stop"
                    assert details.json()["report_message_id"] == report_message_id

                if worker is not None:
                    await worker.stop()
                if worker_task is not None:
                    await asyncio.wait_for(worker_task, timeout=5)
                    worker_task = None

                assert len(provider.report_requests) == 1
                assert provider.main_stream_calls == 0
                expected_fail_calls = 2 if inject_transaction_failure else 1
                assert len(run_repository.fail_commands) == expected_fail_calls
                assert injected_failures == ([1] if inject_transaction_failure else [])
                first_fail_command = run_repository.fail_commands[0]
                assert all(
                    command == first_fail_command for command in run_repository.fail_commands
                )
                assert first_fail_command.report_message is not None
                assert first_fail_command.report_message.id == report_message_id
                assert first_fail_command.safe_error_code == "run_context_safety_stop"
                assert first_fail_command.context_stop_reason is (
                    ContextStopReason.HARD_WINDOW_EXCEEDED
                )
                assert len(first_fail_command.usage.attempts) == 1
                report_runtime, report_request = provider.report_requests[0]
                assert report_request.tools == ()
                assert report_request.tool_choice == "none"
                assert report_request.max_output_tokens is not None
                report_text = "\n".join(message.content for message in report_request.messages)
                assert trigger_text in report_text
                assert history_marker not in report_text
                encoded_report = json.dumps(
                    [
                        {
                            "role": message.role.value,
                            "content": message.content,
                        }
                        for message in report_request.messages
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                report_input_tokens = (
                    await count_plain_text_tokens_async(
                        encoded_report,
                        report_runtime.version,
                        timeout_seconds=5,
                    )
                    + PROTOCOL_TOKEN_RESERVE
                )
                assert (
                    report_input_tokens + report_request.max_output_tokens
                    <= report_runtime.version.context_window_tokens
                )

                async with session_factory() as session:
                    run_row = await session.get(RunModel, run_id)
                    assert run_row is not None
                    assert run_row.status == RunStatus.FAILED.value
                    assert run_row.safe_error_code == "run_context_safety_stop"
                    assert run_row.report_message_id == report_message_id
                    assert run_row.owner_id == user_id
                    assert run_row.llm_request_count == 1
                    usage = run_row.usage
                    usage_attempts = usage["attempts"]
                    assert isinstance(usage_attempts, list)
                    assert len(usage_attempts) == 1

                    trigger_row = await session.get(
                        ConversationMessageModel,
                        trigger_message_id,
                    )
                    report_row = await session.get(
                        ConversationMessageModel,
                        report_message_id,
                    )
                    assert trigger_row is not None
                    assert report_row is not None
                    report_owner_id = await session.scalar(
                        select(SessionModel.owner_id).where(
                            SessionModel.id == report_row.session_id
                        )
                    )
                    assert report_owner_id == run_row.owner_id
                    assert report_row.run_id == run_id
                    assert report_row.role == "assistant"
                    assert report_row.status == "completed"
                    assert report_row.reply_to_message_id == trigger_message_id
                    assert report_row.context_turn_id == trigger_message_id
                    assert report_row.sequence > trigger_row.sequence
                    assert report_row.metadata_json["provider"] == "system_context_safety"
                    assert len(report_row.parts) == 1
                    assert report_row.parts[0]["type"] == "text"
                    assert "任务未标记为完成" in str(report_row.parts[0]["text"])
                    report_message_count = await session.scalar(
                        select(func.count())
                        .select_from(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.run_id == run_id,
                            ConversationMessageModel.role == "assistant",
                        )
                    )
                    assert report_message_count == 1

                    persisted_events = (
                        await session.scalars(
                            select(RunEventModel)
                            .where(RunEventModel.run_id == run_id)
                            .order_by(RunEventModel.sequence)
                        )
                    ).all()
                    failed_events = [event for event in persisted_events if event.type == "failed"]
                    assert len(failed_events) == 1
                    assert failed_events[0].payload["safe_error_code"] == (
                        "run_context_safety_stop"
                    )
                    assert failed_events[0].payload["report_message_id"] == report_message_id
                    assert failed_events[0].payload["context_stop_reason"] == (
                        ContextStopReason.HARD_WINDOW_EXCEEDED.value
                    )
                    assert not any(event.type == "completed" for event in persisted_events)
                    tool_call_count = await session.scalar(
                        select(func.count())
                        .select_from(RunToolCallModel)
                        .where(RunToolCallModel.run_id == run_id)
                    )
                    assert tool_call_count == 0
            finally:
                if transaction_fault_hook is not None:
                    db_event.remove(
                        engine.sync_engine,
                        "before_cursor_execute",
                        transaction_fault_hook,
                    )
                if worker is not None:
                    await worker.stop()
                if worker_task is not None:
                    worker_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker_task
                await api_transport.aclose()
                await worker_transport.aclose()
                await redis_client.aclose()

    asyncio.run(scenario())
