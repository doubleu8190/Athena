"""公开 HTTP、真实 PostgreSQL Worker、Redis 与 SSE 的最小闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from redis.asyncio import Redis
from sqlalchemy import select

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model.profile import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunEventModel, SessionContextSummaryModel
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from api.runs import build_run_router
from app.auth import AuthService
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.run.worker import RunWorker
from domain.llm.model import (
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmUsage,
    ResponseCompleted,
    TextDelta,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.model.types import LlmRequest
from domain.run.model import RunEvent, RunStatus
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


class _FakeStreamingProvider:
    """执行真实 RunExecutor 协议，但不访问外部模型服务。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.compression_requests: list[LlmRequest] = []

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        """以确定性文本响应执行真实的非流式摘要端口。"""
        self.compression_requests.append(request)
        return LlmResponse(
            output=(LlmText("Herbs need sun and water."),),
            status="completed",
            incomplete_reason=None,
            model=runtime.version.model,
            response_id=f"fake-summary-{len(self.compression_requests)}",
            request_id="fake-summary-request",
            usage=LlmUsage(input_tokens=20, output_tokens=9, total_tokens=29),
        )

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        async def events() -> AsyncIterator[LlmStreamEvent]:
            self.started.set()
            yield TextDelta(0, 0, "live provider delta")
            yield ResponseCompleted(
                LlmResponse(
                    output=(LlmText("durable provider reply"),),
                    status="completed",
                    incomplete_reason=None,
                    model="fake-stream-model",
                    response_id="fake-response",
                    request_id="fake-request",
                    usage=LlmUsage(input_tokens=12, output_tokens=4, total_tokens=16),
                )
            )

        yield events()


def test_compressed_public_run_worker_and_sse_recover_missed_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实压缩策略、Worker、PG 和 Redis SSE 在发布丢失后恢复摘要事件。"""
    database_url = DATABASE_URL
    redis_url = REDIS_URL
    if database_url is None or redis_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL and ATHENA_REDIS_URL are required")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_v2_run_e2e") as db:
            factory = db.session_factory
            redis_client = Redis.from_url(
                redis_url, decode_responses=False, socket_connect_timeout=1
            )
            api_transport: RedisRunTransport | None = None
            worker_transport: RedisRunTransport | None = None
            worker_task: asyncio.Task[None] | None = None
            http_client: httpx.AsyncClient | None = None
            try:
                now = datetime.now(UTC)
                token = "run-e2e-session-token"
                async with factory() as session, session.begin():
                    session.add(
                        UserModel(
                            id="usr_run_e2e",
                            email="run-e2e@example.test",
                            password_hash="not-used",
                            display_name=None,
                            status="active",
                            role="member",
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
                    await session.flush()
                    session.add(
                        AuthSessionModel(
                            id="auth_run_e2e",
                            user_id="usr_run_e2e",
                            token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            expires_at=now + timedelta(hours=1),
                            revoked_time=None,
                            created_at=now,
                        )
                    )
                    session.add(
                        SessionModel(
                            id="ses_run_e2e",
                            owner_id="usr_run_e2e",
                            title=None,
                            project_id=None,
                            last_message_sequence=2,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
                    session.add(
                        ModelProfileVersionModel(
                            id="llmprof_run_e2e",
                            name="E2E fake streaming provider",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="fake-stream-model",
                            **encrypted_llm_key_fields("llmprof_run_e2e"),
                            context_window_tokens=2048,
                            tokenizer_encoding="o200k_base",
                            supports_stream=True,
                            supports_tools=True,
                            max_output_tokens=64,
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
                        DefaultModelProfileModel(singleton_id=1, profile_id="llmprof_run_e2e")
                    )
                    session.add(
                        ModelProfileVersionModel(
                            id="llmprof_compression_e2e",
                            name="E2E fake summary provider",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="fake-summary-model",
                            **encrypted_llm_key_fields("llmprof_compression_e2e"),
                            context_window_tokens=8192,
                            tokenizer_encoding="o200k_base",
                            supports_stream=False,
                            supports_tools=False,
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
                        CompressionPolicyVersionModel(
                            id="cmpol_run_e2e",
                            compression_model_profile_id="llmprof_compression_e2e",
                            trigger_ratio=0.4,
                            trigger_tokens=1_024,
                            recent_ratio=0.2,
                            recent_token_cap=2_048,
                            summary_ratio=0.1,
                            summary_token_cap=1_024,
                            prompt_template_version="context-summary-v1",
                            created_at=now,
                        )
                    )
                    await session.flush()
                    session.add(
                        CurrentCompressionPolicyModel(
                            singleton_id=1, policy_version_id="cmpol_run_e2e"
                        )
                    )
                    # 两条旧历史消息超过主模型提前触发阈值，并且都早于本次 Run 的 trigger。
                    session.add_all(
                        [
                            ConversationMessageModel(
                                id="msg_compression_history_user",
                                session_id="ses_run_e2e",
                                role="user",
                                status="completed",
                                sequence=1,
                                parts=[
                                    {
                                        "type": "text",
                                        "text": (
                                            "Plan a small herb garden. Explain soil, light, "
                                            "watering, spacing and seasonal care in practical "
                                            "detail. "
                                        )
                                        * 50,
                                    }
                                ],
                                reply_to_message_id=None,
                                run_id=None,
                                managed_run_id=None,
                                context_turn_id=None,
                                client_message_id=None,
                                created_at=now,
                                completed_at=now,
                                metadata_json={},
                            ),
                            ConversationMessageModel(
                                id="msg_compression_history_assistant",
                                session_id="ses_run_e2e",
                                role="assistant",
                                status="completed",
                                sequence=2,
                                parts=[
                                    {
                                        "type": "text",
                                        "text": (
                                            "Water deeply but infrequently, check top soil, "
                                            "and group herbs "
                                            "by sunlight needs. "
                                        )
                                        * 50,
                                    }
                                ],
                                reply_to_message_id="msg_compression_history_user",
                                run_id=None,
                                managed_run_id=None,
                                context_turn_id=None,
                                client_message_id=None,
                                created_at=now,
                                completed_at=now,
                                metadata_json={},
                            ),
                        ]
                    )

                auth_service = AuthService(
                    SqlAlchemyAuthRepository(factory), Argon2PasswordHasher()
                )
                message_repository = SqlAlchemyMessageRepository(factory)
                run_repository = SqlAlchemyRunRepository(factory, message_repository)
                profile_repository = SqlAlchemyModelProfileRepository(factory, TEST_LLM_CIPHER)
                api_transport = RedisRunTransport(Redis.from_url(redis_url, decode_responses=False))
                worker_transport = RedisRunTransport(
                    Redis.from_url(redis_url, decode_responses=False)
                )
                original_publish = worker_transport.publish_persisted

                async def drop_compression_notifications(
                    event_run_id: str, execution_epoch: int, event: RunEvent
                ) -> None:
                    if event.type.startswith("compression_"):
                        return
                    await original_publish(event_run_id, execution_epoch, event)

                # 只丢弃压缩 Pub/Sub 通知，数据库事件仍已提交；SSE 对账应补齐缺失状态。
                monkeypatch.setattr(
                    worker_transport, "publish_persisted", drop_compression_notifications
                )
                run_service = RunService(run_repository)
                stream_service = RunStreamService(run_repository, auth_service, api_transport)
                app = FastAPI()
                app.state.authenticator = auth_service
                app.state.auth_cookie_name = "athena_session"
                app.state.public_origin = None
                app.include_router(build_run_router(run_service, stream_service))
                http_client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                )
                http_client.cookies.set("athena_session", token)

                submitted = await http_client.post(
                    "/api/v1/sessions/ses_run_e2e/runs",
                    json={"request_id": "request-run-e2e", "text": "Answer with a short greeting."},
                )
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["id"]
                assert submitted.json()["status"] == "queued"

                stream_task = asyncio.create_task(
                    http_client.get(
                        f"/api/v1/sessions/ses_run_e2e/runs/{run_id}/stream",
                        headers={"Last-Event-ID": "0"},
                        timeout=20,
                    )
                )
                channel = f"athena:run:events:{run_id}"
                deadline = asyncio.get_running_loop().time() + 5
                while True:
                    subscriber_count = (await redis_client.pubsub_numsub(channel))[0][1]
                    if subscriber_count:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("run SSE did not subscribe to Redis")
                    await asyncio.sleep(0.02)

                fake_provider = _FakeStreamingProvider()
                executor = RunExecutor(
                    run_repository,
                    profile_repository,
                    fake_provider,
                    system_prompts=SqlAlchemySystemPromptRepository(factory),
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
                try:
                    await asyncio.wait_for(fake_provider.started.wait(), timeout=10)
                except TimeoutError as error:
                    current_run = await run_repository.get("ses_run_e2e", "usr_run_e2e", run_id)
                    persisted_events = await run_repository.list_events(
                        "ses_run_e2e", "usr_run_e2e", run_id, after_sequence=0, limit=100
                    )
                    raise AssertionError(
                        "fake main model did not start; "
                        f"status={None if current_run is None else current_run.status}, "
                        f"compression_requests={len(fake_provider.compression_requests)}, "
                        f"events={[(event.type, event.payload) for event in persisted_events]}"
                    ) from error
                stream_response = await asyncio.wait_for(stream_task, timeout=15)
                assert stream_response.status_code == 200
                assert "live provider delta" in stream_response.text
                assert "durable provider reply" in stream_response.text
                assert '"status":"completed"' in stream_response.text
                assert "compression_started" in stream_response.text
                assert "compression_completed" in stream_response.text
                assert "Herbs need sun and water." not in stream_response.text
                assert len(fake_provider.compression_requests) == 1

                completed = await run_repository.get("ses_run_e2e", "usr_run_e2e", run_id)
                assert completed is not None and completed.status is RunStatus.COMPLETED
                async with factory() as session:
                    assistant_messages = (
                        await session.scalars(
                            select(ConversationMessageModel).where(
                                ConversationMessageModel.run_id == run_id,
                                ConversationMessageModel.role == "assistant",
                            )
                        )
                    ).all()
                assert len(assistant_messages) == 1
                assert assistant_messages[0].parts == [
                    {"type": "text", "text": "durable provider reply"}
                ]
                async with factory() as session:
                    persisted_summary = await session.scalar(
                        select(SessionContextSummaryModel).where(
                            SessionContextSummaryModel.session_id == "ses_run_e2e"
                        )
                    )
                    compression_events = (
                        await session.scalars(
                            select(RunEventModel).where(
                                RunEventModel.run_id == run_id,
                                RunEventModel.type.in_(
                                    ["compression_started", "compression_completed"]
                                ),
                            )
                        )
                    ).all()
                assert persisted_summary is not None
                assert (
                    persisted_summary.covered_through_sequence
                    < submitted.json()["context_cutoff_sequence"]
                )
                assert len(compression_events) == 2
                await worker.stop()
                await asyncio.wait_for(worker_task, timeout=5)
                worker_task = None
            finally:
                if worker_task is not None:
                    worker_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                if http_client is not None:
                    await http_client.aclose()
                if api_transport is not None:
                    await api_transport.aclose()
                if worker_transport is not None:
                    await worker_transport.aclose()
                await redis_client.aclose()

    asyncio.run(scenario())
