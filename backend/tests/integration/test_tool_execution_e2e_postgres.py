"""真实 PostgreSQL Run 工具循环、API 查询与 unknown retry 的端到端测试。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx2
import pytest
from access_support import make_test_access_policy
from fastapi import FastAPI
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.preference import SqlAlchemyPreferenceRepository
from adapter.project.model import ProjectModel
from adapter.run.model import RunEventModel, RunToolCallModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from api.runs import build_run_router
from app.auth import AuthService
from app.preference import PreferenceService
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.tool.runtime import RunToolRuntimeService
from domain.knowledge.model import ChunkLocator, KnowledgeBase, SearchHit, SearchResult
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    StreamableHttpConfig,
)
from domain.run.model import RunStatus
from domain.tool import (
    McpToolSource,
    RuntimeToolDefinition,
    ToolCallStatus,
    ToolOutcomeStatus,
)
from infrastructure.security import Argon2PasswordHasher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


@asynccontextmanager
async def _database() -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], object, object, str], None
]:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    async with isolated_postgres_schema(database_url, prefix="athena_v2_tool_e2e") as db:
        yield db.session_factory, db.engine, db.admin_engine, db.schema


class _ProjectKnowledge:
    def __init__(self, owner_id: str, project_id: str) -> None:
        self.owner_id = owner_id
        self.project_id = project_id
        self.now = datetime.now(UTC)

    async def list(
        self,
        owner_id: str,
        project_id: str,
        *,
        snapshot: Any = None,
    ) -> list[KnowledgeBase]:
        del snapshot
        assert (owner_id, project_id) == (self.owner_id, self.project_id)
        return [
            KnowledgeBase(
                id="kbase_e2e",
                owner_id=self.owner_id,
                name="E2E knowledge",
                description=None,
                created_at=self.now,
                updated_at=self.now,
            )
        ]


class _KnowledgeSearch:
    def __init__(self) -> None:
        self.requests: list[tuple[str, tuple[str, ...], str]] = []

    async def search(
        self,
        owner_id: str,
        base_ids: list[str],
        query: str,
        *,
        limit: int,
        context_budget: int,
        skip_access_check: bool = False,
    ) -> SearchResult:
        del skip_access_check
        assert limit == 5
        assert context_budget == 2000
        self.requests.append((owner_id, tuple(base_ids), query))
        return SearchResult(
            hits=(
                SearchHit(
                    chunk_id="chunk_e2e",
                    document_id="document_e2e",
                    version_id="version_e2e",
                    title="Runbook",
                    text="The deployment checklist is complete.",
                    locators=(ChunkLocator(kind="page", page=1),),
                    channels=("keyword",),
                    rank=1.0,
                    knowledge_base_id="kbase_e2e",
                ),
            ),
            channels=("keyword",),
        )


class _McpCatalog:
    def __init__(self) -> None:
        self.schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        self.definition = RuntimeToolDefinition(
            tool_id="tool_e2e_probe",
            model_name="mcp_probe",
            description="Read the remote E2E marker.",
            input_schema=self.schema,
            source=McpToolSource(
                server_id="server_e2e",
                remote_name="remote_probe",
                config_version=1,
            ),
        )
        self.target = McpRuntimeToolTarget(
            tool_id=self.definition.tool_id,
            server_id="server_e2e",
            remote_name="remote_probe",
            config_version=1,
            connection=McpConnectionInput(
                StreamableHttpConfig(
                    transport="streamable_http",
                    url="https://mcp.example.test/mcp",
                ),
                {},
            ),
        )

    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
        assert limit == 201
        return (self.definition,)

    async def list_readable_runtime_tools(
        self,
        snapshot: Any,
        *,
        limit: int = 200,
    ) -> tuple[RuntimeToolDefinition, ...]:
        del snapshot
        return await self.list_runtime_tools(limit=limit)

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        if (
            tool_id,
            server_id,
            remote_name,
            expected_config_version,
        ) == (
            self.target.tool_id,
            self.target.server_id,
            self.target.remote_name,
            self.target.config_version,
        ):
            return self.target
        return None


class _McpExecutor:
    def __init__(self, *, status: McpCallStatus = McpCallStatus.OK) -> None:
        self.status = status
        self.calls: list[dict[str, object]] = []

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        assert deadline_seconds > 0
        self.calls.append(dict(arguments))
        if self.status is McpCallStatus.UNKNOWN:
            return McpCallResult(
                McpCallStatus.UNKNOWN,
                "mcp_tool_timeout",
                "MCP result is unknown.",
            )
        return McpCallResult(
            McpCallStatus.OK,
            "mcp_tool_completed",
            "remote result",
            structured={"answer": "remote marker", "target": target.remote_name},
        )


class _ScriptedProvider:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        del runtime
        self.requests.append(request)
        response = self.responses.pop(0)

        async def events() -> AsyncIterator[LlmStreamEvent]:
            yield ResponseCompleted(response)

        yield events()

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        del runtime, request
        raise AssertionError("complete is not expected in this test")


def _response(
    *,
    call: tuple[str, str, dict[str, object]] | None = None,
    text: str = "",
    attempt: int,
) -> LlmResponse:
    output: list[LlmText | LlmToolCall] = []
    if text:
        output.append(LlmText(text))
    if call is not None:
        output.append(LlmToolCall(*call))
    return LlmResponse(
        output=tuple(output),
        status="completed",
        incomplete_reason=None,
        model="tool-integration-model",
        response_id=f"resp_{attempt}",
        request_id=None,
        usage=LlmUsage(input_tokens=10, output_tokens=2, total_tokens=12),
    )


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    now = datetime.now(UTC)
    owner_id = "usr_tool_e2e"
    session_id = "ses_tool_e2e"
    project_id = "prj_tool_e2e"
    auth_session_id = "auth_tool_e2e"
    token = "tool-e2e-token"
    async with factory() as session, session.begin():
        session.add(
            UserModel(
                id=owner_id,
                email="tool-e2e@example.test",
                password_hash="unused",
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
                id=auth_session_id,
                user_id=owner_id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(hours=1),
                revoked_time=None,
                created_at=now,
            )
        )
        session.add(
            ProjectModel(
                id=project_id,
                owner_id=owner_id,
                name="Tool E2E",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                owner_id=owner_id,
                title=None,
                project_id=project_id,
                last_message_sequence=0,
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            ModelProfileVersionModel(
                id="llmprof_tool_e2e",
                name="Tool E2E profile",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="tool-integration-model",
                **encrypted_llm_key_fields("llmprof_tool_e2e", "test-only-credential"),
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
            )
        )
        await session.flush()
        session.add(DefaultModelProfileModel(singleton_id=1, profile_id="llmprof_tool_e2e"))
    return owner_id, session_id, project_id, token


async def _api_client(
    app: FastAPI,
    token: str,
) -> httpx2.AsyncClient:
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    )
    client.cookies.set("athena_session", token)
    return client


def test_mcp_builtin_loop_queries_events_and_unknown_retry_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Run 仓储串联三种工具，持久事件可查询，unknown 停止后 API retry 需整体确认。"""

    async def scenario(monkeypatch: pytest.MonkeyPatch) -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, session_id, project_id, token = await _seed(factory)
            auth_repository = SqlAlchemyAuthRepository(factory)
            session_repository = SqlAlchemySessionRepository(factory)
            message_repository = SqlAlchemyMessageRepository(factory)
            run_repository = SqlAlchemyRunRepository(factory, message_repository)
            profile_repository = SqlAlchemyModelProfileRepository(factory, TEST_LLM_CIPHER)
            auth_service = AuthService(auth_repository, Argon2PasswordHasher())
            run_service = RunService(run_repository)
            app = FastAPI()
            app.state.authenticator = auth_service
            app.state.auth_cookie_name = "athena_session"
            app.state.public_origin = None
            app.include_router(build_run_router(run_service))
            client = await _api_client(app, token)
            mcp_catalog = _McpCatalog()
            successful_mcp = _McpExecutor()
            preferences = PreferenceService(SqlAlchemyPreferenceRepository(factory))
            project_knowledge = _ProjectKnowledge(owner_id, project_id)
            knowledge = _KnowledgeSearch()
            runtime = RunToolRuntimeService(
                auth=auth_repository,
                sessions=session_repository,
                mcp_catalog=mcp_catalog,
                mcp_executor=successful_mcp,
                knowledge=cast(Any, knowledge),
                project_knowledge=cast(Any, project_knowledge),
                preferences=preferences,
                access_policy=make_test_access_policy(),
            )
            provider = _ScriptedProvider(
                [
                    _response(
                        call=("call_mcp", "mcp_probe", {"query": "remote"}),
                        attempt=1,
                    ),
                    _response(
                        call=(
                            "call_memory",
                            "save_user_memory",
                            {"content": "偏好简洁结论"},
                        ),
                        attempt=2,
                    ),
                    _response(
                        call=("call_knowledge", "knowledge_search", {"query": "checklist"}),
                        attempt=3,
                    ),
                    _response(text="The three tool results are consistent.", attempt=4),
                ]
            )
            executor = RunExecutor(
                run_repository,
                profile_repository,
                provider,
                system_prompts=SqlAlchemySystemPromptRepository(factory),
                tool_runtime=runtime,
                preferences=preferences,
                config=RunWorkerConfig(),
            )
            try:
                submitted = await client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "tool-e2e-success",
                        "text": "Use the available tools and answer.",
                    },
                )
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["id"]
                leases = await run_repository.claim(
                    "worker-tool-e2e",
                    lease_seconds=30,
                    execution_seconds=60,
                    limit=1,
                )
                assert len(leases) == 1 and leases[0].run.id == run_id
                await executor.execute(leases[0])

                completed = await run_repository.get(session_id, owner_id, run_id)
                assert completed is not None
                assert completed.status is RunStatus.COMPLETED
                assert completed.tool_snapshot is not None
                assert len(provider.requests) == 4
                assert len(successful_mcp.calls) == 1
                assert successful_mcp.calls[0] == {"query": "remote"}
                saved_memories = await preferences.list_memories(owner_id)
                assert saved_memories.total == 1
                assert saved_memories.items[0].content == "偏好简洁结论"
                assert saved_memories.items[0].source.value == "chat_explicit"
                assert knowledge.requests == [(owner_id, ("kbase_e2e",), "checklist")]
                assert len(completed.usage.attempts) == 4
                assert completed.usage.total_tokens == 48
                assert completed.llm_request_count == 4

                async with factory() as session:
                    calls = (
                        await session.scalars(
                            select(RunToolCallModel)
                            .where(RunToolCallModel.run_id == run_id)
                            .order_by(RunToolCallModel.ordinal)
                        )
                    ).all()
                    assistant = (
                        await session.scalars(
                            select(ConversationMessageModel).where(
                                ConversationMessageModel.run_id == run_id,
                                ConversationMessageModel.role == "assistant",
                            )
                        )
                    ).all()
                    results = (
                        await session.scalars(
                            select(ExecutionMessageModel).where(
                                ExecutionMessageModel.run_id == run_id,
                                ExecutionMessageModel.role == "tool",
                            )
                        )
                    ).all()
                    started_events = (
                        await session.scalars(
                            select(RunEventModel)
                            .where(
                                RunEventModel.run_id == run_id,
                                RunEventModel.type == "tool_call_started",
                            )
                            .order_by(RunEventModel.sequence)
                        )
                    ).all()
                    finished_events = (
                        await session.scalars(
                            select(RunEventModel)
                            .where(
                                RunEventModel.run_id == run_id,
                                RunEventModel.type == "tool_call_finished",
                            )
                            .order_by(RunEventModel.sequence)
                        )
                    ).all()
                assert [call.model_name for call in calls] == [
                    "mcp_probe",
                    "save_user_memory",
                    "knowledge_search",
                ]
                assert [call.status for call in calls] == [
                    ToolCallStatus.COMPLETED.value,
                    ToolCallStatus.COMPLETED.value,
                    ToolCallStatus.COMPLETED.value,
                ]
                assert all(call.result_message_id is not None for call in calls)
                assert len(results) == 3
                assert len(assistant) == 1
                assert assistant[0].parts == [
                    {"type": "text", "text": "The three tool results are consistent."}
                ]
                assert [event.payload["round"] for event in started_events] == [1, 2, 3]
                assert [event.payload["round"] for event in finished_events] == [1, 2, 3]

                first_page = await client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls",
                    params={"limit": 2},
                )
                second_page = await client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls",
                    params={"limit": 2, "after_ordinal": 2},
                )
                details = await client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{calls[0].id}"
                )
                events = await client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/events",
                    params={"after_sequence": 0, "limit": 100},
                )
                assert first_page.status_code == 200
                assert first_page.json()["next_after_ordinal"] == 2
                assert second_page.json()["items"][0]["model_name"] == "knowledge_search"
                assert second_page.json()["items"][0]["display_name"] == "查阅企业知识库"
                assert details.status_code == 200
                detail_json = details.json()
                assert "arguments" not in detail_json
                assert detail_json["formatted_arguments"] == []
                assert detail_json["hidden_argument_count"] == 1
                assert detail_json["outcome"]["status"] == "ok"
                assert detail_json["outcome"]["summary"] == "已完成业务工具调用。"
                assert detail_json["outcome"]["formatted_results"] == []
                assert "remote" not in details.text
                assert "server_e2e" not in details.text
                event_types = [item["type"] for item in events.json()["items"]]
                assert event_types.count("tool_call_started") == 3
                assert event_types.count("tool_call_finished") == 3

                unknown_provider = _ScriptedProvider(
                    [
                        _response(
                            call=("call_unknown", "mcp_probe", {"query": "unknown"}),
                            attempt=1,
                        )
                    ]
                )
                unknown_mcp = _McpExecutor(status=McpCallStatus.UNKNOWN)
                unknown_runtime = RunToolRuntimeService(
                    auth=auth_repository,
                    sessions=session_repository,
                    mcp_catalog=mcp_catalog,
                    mcp_executor=unknown_mcp,
                    knowledge=cast(Any, knowledge),
                    project_knowledge=cast(Any, project_knowledge),
                    preferences=preferences,
                    access_policy=make_test_access_policy(),
                )
                unknown_executor = RunExecutor(
                    run_repository,
                    profile_repository,
                    unknown_provider,
                    system_prompts=SqlAlchemySystemPromptRepository(factory),
                    tool_runtime=unknown_runtime,
                    config=RunWorkerConfig(),
                )
                unknown_submitted = await client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "tool-e2e-unknown",
                        "text": "This call will be unknown.",
                    },
                )
                assert unknown_submitted.status_code == 201, unknown_submitted.text
                unknown_run_id = unknown_submitted.json()["id"]
                unknown_leases = await run_repository.claim(
                    "worker-tool-e2e",
                    lease_seconds=30,
                    execution_seconds=60,
                    limit=1,
                )
                assert len(unknown_leases) == 1
                await unknown_executor.execute(unknown_leases[0])

                unknown_run = await run_repository.get(
                    session_id,
                    owner_id,
                    unknown_run_id,
                )
                assert unknown_run is not None
                assert unknown_run.status is RunStatus.FAILED
                assert unknown_run.safe_error_code == "tool_outcome_unknown"
                assert len(unknown_provider.requests) == 1
                assert len(unknown_mcp.calls) == 1
                unknown_page = await run_repository.list_tool_calls(
                    session_id,
                    owner_id,
                    unknown_run_id,
                    after_ordinal=0,
                    limit=1,
                )
                assert unknown_page is not None and unknown_page.items
                unknown_details = await run_repository.get_tool_call(
                    session_id,
                    owner_id,
                    unknown_run_id,
                    unknown_page.items[0].id,
                )
                assert unknown_details is not None
                assert unknown_details.record.status is ToolCallStatus.UNKNOWN
                assert unknown_details.record.outcome is not None
                assert unknown_details.record.outcome.status is ToolOutcomeStatus.UNKNOWN

                unconfirmed = await client.post(
                    f"/api/v1/sessions/{session_id}/runs/{unknown_run_id}/retry",
                    json={"request_id": "retry-unconfirmed"},
                )
                confirmed = await client.post(
                    f"/api/v1/sessions/{session_id}/runs/{unknown_run_id}/retry",
                    json={
                        "request_id": "retry-confirmed",
                        "confirm_unknown": True,
                    },
                )
                assert unconfirmed.status_code == 409
                assert unconfirmed.json() == {"detail": "tool_retry_confirmation_required"}
                assert confirmed.status_code == 200, confirmed.text
                assert len(unknown_provider.requests) == 1
                assert len(unknown_mcp.calls) == 1
                retried = await run_repository.get(
                    session_id,
                    owner_id,
                    confirmed.json()["id"],
                )
                assert retried is not None
                assert retried.retry_of_run_id == unknown_run_id
                assert retried.llm_request_count == 0
                assert confirmed.json()["retry_warning"]
                old_details = await run_repository.get_tool_call(
                    session_id,
                    owner_id,
                    unknown_run_id,
                    unknown_details.record.id,
                )
                assert old_details is not None
                assert old_details.record.status is ToolCallStatus.UNKNOWN
            finally:
                await client.aclose()

    asyncio.run(scenario(monkeypatch))
