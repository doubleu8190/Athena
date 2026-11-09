"""公开 HTTP、真实 PostgreSQL Worker、Redis 与 SSE 的最小闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
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
from domain.run.model import RunStatus
from infrastructure.security import Argon2PasswordHasher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
REDIS_URL = os.getenv("ATHENA_REDIS_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
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

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
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


def test_public_submit_worker_redis_sse_and_persisted_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Cookie/HTTP 和 PG Worker 经 Redis SSE 交付正式结果。"""
    database_url = DATABASE_URL
    redis_url = REDIS_URL
    if database_url is None or redis_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL and ATHENA_REDIS_URL are required")

    async def scenario() -> None:
        schema = f"athena_v2_run_e2e_{token_hex(8)}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        redis_client = Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=1)
        api_transport: RedisRunTransport | None = None
        worker_transport: RedisRunTransport | None = None
        worker_task: asyncio.Task[None] | None = None
        http_client: httpx2.AsyncClient | None = None
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            config = Config(str(ALEMBIC_INI))
            config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, config, "head")

            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(UTC)
            token = "run-e2e-session-token"
            credential_env = "ATHENA_LLM_CREDENTIAL_E2E_TEST"
            monkeypatch.setenv(credential_env, "test-only-fake-provider-credential")
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_run_e2e",
                        email="run-e2e@example.test",
                        password_hash="not-used",
                        display_name=None,
                        status="active",
                        role="user",
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
                        last_message_sequence=0,
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
                        credential_env=credential_env,
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
                    )
                )
                await session.flush()
                session.add(DefaultModelProfileModel(singleton_id=1, profile_id="llmprof_run_e2e"))

            auth_service = AuthService(SqlAlchemyAuthRepository(factory), Argon2PasswordHasher())
            message_repository = SqlAlchemyMessageRepository(factory)
            run_repository = SqlAlchemyRunRepository(factory, message_repository)
            profile_repository = SqlAlchemyModelProfileRepository(factory)
            api_transport = RedisRunTransport(Redis.from_url(redis_url, decode_responses=False))
            worker_transport = RedisRunTransport(Redis.from_url(redis_url, decode_responses=False))
            run_service = RunService(run_repository)
            stream_service = RunStreamService(run_repository, auth_service, api_transport)
            app = FastAPI()
            app.state.authenticator = auth_service
            app.state.auth_cookie_name = "athena_session"
            app.state.public_origin = None
            app.include_router(build_run_router(run_service, stream_service))
            http_client = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
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
            await asyncio.wait_for(fake_provider.started.wait(), timeout=10)
            stream_response = await asyncio.wait_for(stream_task, timeout=15)
            assert stream_response.status_code == 200
            assert "live provider delta" in stream_response.text
            assert "durable provider reply" in stream_response.text
            assert '"status":"completed"' in stream_response.text

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
            await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
