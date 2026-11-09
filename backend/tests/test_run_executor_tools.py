"""Run Executor 的工具停止边界和最后额度语义。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import types

from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.tool.runtime import RunToolRuntimeService
from domain.auth.model import User, UserRole, UserStatus
from domain.llm.model import (
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolDefinition,
    McpRuntimeToolTarget,
    StdioConfig,
)
from domain.message import ToolCallPart, ToolResultPart
from domain.run.model import (
    Run,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunRepository
from domain.session.model import Session
from domain.tool import (
    AuthorizedRunContext,
    BeginToolCallCommand,
    BeginToolCallResult,
    BuiltinToolSource,
    FinishToolCallCommand,
    McpToolSource,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolCallRecord,
    RunToolRuntime,
    ToolCallStatus,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
)
from infrastructure.mcp.execution import McpToolCallClient

NOW = datetime.now(UTC)


def _runtime_profile() -> RuntimeModelProfile:
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id="profile-tools",
            name="tools",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="tools-model",
            credential_env="ATHENA_LLM_CREDENTIAL_TOOLS",
            context_window_tokens=8192,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=256,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=NOW,
        ),
        api_key="test-secret",
    )


def _definition() -> RuntimeToolDefinition:
    return RuntimeToolDefinition(
        tool_id="builtin_test_tool",
        model_name="test_tool",
        description="test tool",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source=BuiltinToolSource(version=1),
    )


def _mcp_definition() -> RuntimeToolDefinition:
    return RuntimeToolDefinition(
        tool_id="tool_lookup",
        model_name="test_tool",
        description="test MCP tool",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source=McpToolSource(server_id="mcp_1", remote_name="remote_lookup", config_version=1),
    )


def _snapshot(definition: RuntimeToolDefinition | None = None) -> RuntimeToolSnapshot:
    return RuntimeToolSnapshot(tools=(definition or _definition(),))


def _lease(
    *,
    used: int = 0,
    definition: RuntimeToolDefinition | None = None,
) -> RunLease:
    run = Run(
        id="run-tools",
        session_id="session-tools",
        owner_id="owner-tools",
        status=RunStatus.RUNNING,
        trigger_message_id="message-tools",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile-tools",
        prompt_template_version="run-v2",
        event_sequence=0,
        execution_epoch=1,
        worker_id="worker-tools",
        lease_expires_at=NOW + timedelta(minutes=2),
        execution_started_at=NOW,
        execution_deadline_at=NOW + timedelta(minutes=1),
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        tool_snapshot=_snapshot(definition),
        auth_session_id="auth-tools",
        llm_request_count=used,
    )
    return RunLease(run, run.lease_expires_at or NOW, run.execution_deadline_at or NOW)


class _ToolRuntime:
    def __init__(self, result: ToolExecutionResult, *, delay_seconds: float = 0.0) -> None:
        self.result = result
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def authorize(
        self,
        owner_id: str,
        chat_session_id: str,
        run_id: str,
        auth_session_id: str | None,
    ) -> AuthorizedRunContext:
        return AuthorizedRunContext(owner_id, chat_session_id, None, auth_session_id or "")

    async def build_snapshot(
        self, prompt_template_version: str, context: AuthorizedRunContext
    ) -> RuntimeToolSnapshot:
        return _snapshot()

    async def execute(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        context: object,
        *,
        deadline_seconds: float,
        run_remaining_seconds: float,
    ) -> ToolExecutionResult:
        del run_remaining_seconds
        self.calls += 1
        await asyncio.sleep(self.delay_seconds)
        return self.result


class _Repository:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.begin_tool_calls = 0
        self.finish_tool_calls = 0
        self.normal_finishes = 0
        self.failures: list[str] = []

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        return True

    async def list_retry_context(self, *args: object) -> tuple[object, ...]:
        return ()

    async def list_context_messages(self, *args: object) -> tuple[object, ...]:
        return ()

    async def reserve_llm_request(self, command: Any) -> int:
        self.run = replace(self.run, llm_request_count=self.run.llm_request_count + 1)
        return self.run.llm_request_count

    async def record_usage(self, command: object) -> Run:
        return self.run

    async def begin_tool_call(self, command: BeginToolCallCommand) -> BeginToolCallResult:
        self.begin_tool_calls += 1
        self.last_begin_command = command
        record = RunToolCallRecord(
            id=command.record_id,
            run_id=command.run_id,
            session_id=command.call_message.session_id,
            tool_call_id=command.tool_call_id,
            ordinal=1,
            tool_id=command.tool_id,
            tool_kind=command.tool_kind,
            model_name=command.model_name,
            execution_epoch=command.execution_epoch,
            status=ToolCallStatus.EXECUTING,
            call_message_id=command.call_message.id,
            result_message_id=None,
            started_at=command.started_at,
            finished_at=None,
            safe_error_code=None,
            outcome=None,
        )
        return BeginToolCallResult(record, True)

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run:
        self.finish_tool_calls += 1
        self.last_finish_command = command
        if (
            command.outcome.status is ToolOutcomeStatus.UNKNOWN
            or command.terminate_error_code is not None
        ):
            self.run = replace(
                self.run,
                status=RunStatus.FAILED,
                safe_error_code=(
                    "tool_outcome_unknown"
                    if command.outcome.status is ToolOutcomeStatus.UNKNOWN
                    else command.terminate_error_code
                ),
            )
        return self.run

    async def finish(self, command: object) -> Run:
        self.normal_finishes += 1
        self.run = replace(self.run, status=RunStatus.COMPLETED)
        return self.run

    async def fail(self, command: Any) -> Run:
        self.failures.append(command.safe_error_code)
        self.run = replace(self.run, status=RunStatus.FAILED)
        return self.run


class _Profiles:
    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
        return _runtime_profile()


class _DeliveryAuth:
    def __init__(self, *, block_after_request: int, block_seconds: float) -> None:
        self.block_after_request = block_after_request
        self.block_seconds = block_seconds
        self.requests = 0
        self.post_tool_started = asyncio.Event()

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del session_id, now
        self.requests += 1
        if self.requests >= self.block_after_request:
            self.post_tool_started.set()
            await asyncio.sleep(self.block_seconds)
        return User(
            id=user_id,
            email="owner@example.com",
            password_hash="hash",
            display_name=None,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            role=UserRole.USER,
        )


class _DeliverySessions:
    async def get_visible(self, scope: Any) -> Session | None:
        return Session(
            id=scope.session_id,
            owner_id=scope.owner_id,
            title=None,
            project_id=None,
            last_message_sequence=0,
            created_at=NOW,
            updated_at=NOW,
            deleted_time=None,
        )


class _DeliveryMcpCatalog:
    async def list_runtime_tools(self, *, limit: int) -> tuple[McpRuntimeToolDefinition, ...]:
        del limit
        return ()

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        del tool_id, server_id, remote_name, expected_config_version
        return McpRuntimeToolTarget(
            tool_id="tool_lookup",
            server_id="mcp_1",
            remote_name="remote_lookup",
            config_version=1,
            connection=McpConnectionInput(
                StdioConfig(transport="stdio", command=sys.executable),
                {},
            ),
        )


class _DeliveryMcpExecutor:
    def __init__(
        self,
        result: McpCallResult,
        *,
        business_delay_seconds: float,
        cleanup_delay_seconds: float,
    ) -> None:
        self.result = result
        self.business_delay_seconds = business_delay_seconds
        self.cleanup_delay_seconds = cleanup_delay_seconds
        self.calls = 0

    async def call(
        self,
        target: Any,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del target, arguments
        assert deadline_seconds > 0
        self.calls += 1
        await asyncio.sleep(self.business_delay_seconds)
        await asyncio.sleep(self.cleanup_delay_seconds)
        return self.result


class _RealMcpSession:
    def __init__(self, *, business_delay_seconds: float) -> None:
        self.business_delay_seconds = business_delay_seconds

    async def list_tools(self, *, params: Any = None) -> Any:
        del params
        return SimpleNamespace(
            tools=[SimpleNamespace(name="remote_lookup")],
            next_cursor=None,
        )

    async def call_tool(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(self.business_delay_seconds)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="known external effect")],
            structured_content={"status": "known"},
            is_error=False,
        )


class _RealMcpSlowCloseClient:
    def __init__(self, *, business_delay_seconds: float) -> None:
        self.server_capabilities = SimpleNamespace(tools=object())
        self.session = _RealMcpSession(
            business_delay_seconds=business_delay_seconds,
        )

    async def __aenter__(self) -> _RealMcpSlowCloseClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        await asyncio.sleep(5)


def _delivery_runtime(
    auth: _DeliveryAuth,
    mcp_executor: Any,
) -> RunToolRuntimeService:
    return RunToolRuntimeService(
        auth=cast(Any, auth),
        sessions=cast(Any, _DeliverySessions()),
        mcp_catalog=cast(Any, _DeliveryMcpCatalog()),
        mcp_executor=mcp_executor,
        memory=None,
        memory_enabled=False,
        knowledge=None,
        project_knowledge=None,
    )


class _ScriptedProvider:
    def __init__(self, outcomes: list[LlmResponse | LlmProviderError]) -> None:
        self.outcomes = outcomes
        self.requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)

        async def events() -> AsyncIterator[LlmStreamEvent]:
            if isinstance(outcome, LlmProviderError):
                raise outcome
            yield ResponseCompleted(outcome)

        yield events()


def _response(*, call: LlmToolCall | None = None, text: str = "") -> LlmResponse:
    output: tuple[LlmText | LlmToolCall, ...] = ((LlmText(text),) if text else ()) + (
        (call,) if call is not None else ()
    )
    return LlmResponse(
        output=output,
        status="completed",
        incomplete_reason=None,
        model="tools-model",
        response_id="resp-tools",
        request_id=None,
        usage=LlmUsage(1, 1, 2),
    )


def test_unknown_tool_result_stops_model_and_tool_loop() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_timeout",
                    message="unknown",
                )
            )
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-unknown",
                        "test_tool",
                        {"query": "value"},
                    )
                )
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert tool_runtime.calls == 1
        assert repository.finish_tool_calls == 1
        assert repository.normal_finishes == 0
        assert repository.run.status is RunStatus.FAILED
        assert repository.run.safe_error_code == "tool_outcome_unknown"

    asyncio.run(scenario())


def test_revoked_delivery_stops_after_persisting_known_tool_result() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.OK,
                    code="tool_completed",
                    message="completed",
                    data={"text": "known external effect"},
                ),
                "run_auth_session_invalid",
            )
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-known",
                        "test_tool",
                        {"query": "value"},
                    )
                )
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert tool_runtime.calls == 1
        assert repository.run.safe_error_code == "run_auth_session_invalid"

    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_error_code", [None, "mcp_cleanup_failed"])
def test_mcp_cleanup_budget_keeps_confirmed_success_out_of_unknown(
    cleanup_error_code: str | None,
) -> None:
    """外层工具守护必须容纳 MCP 清理，且保留独立清理标记。"""

    async def scenario() -> None:
        definition = _mcp_definition()
        lease = _lease(definition=definition)
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.OK,
                    code="mcp_tool_completed",
                    message="completed",
                    data={"text": "known external effect"},
                    cleanup_error_code=cleanup_error_code,
                )
            ),
            delay_seconds=0.1,
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-mcp",
                        "test_tool",
                        {"query": "value"},
                    )
                ),
                _response(text="final answer"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(
                tool_timeout_seconds=0.05,
                tool_cleanup_seconds=0.2,
            ),
        )

        await executor.execute(lease)

        assert tool_runtime.calls == 1
        assert len(provider.requests) == 2
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED
        assert repository.run.safe_error_code is None
        assert repository.last_finish_command is not None
        outcome = repository.last_finish_command.outcome
        assert outcome.status is ToolOutcomeStatus.OK
        assert outcome.cleanup_error_code == cleanup_error_code

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mcp_status", "expected_status", "mcp_code"),
    [
        (McpCallStatus.OK, ToolOutcomeStatus.OK, "mcp_tool_completed"),
        (McpCallStatus.ERROR, ToolOutcomeStatus.ERROR, "mcp_tool_error"),
    ],
)
def test_real_runtime_preserves_known_mcp_result_when_delivery_auth_times_out(
    mcp_status: McpCallStatus,
    expected_status: ToolOutcomeStatus,
    mcp_code: str,
) -> None:
    """MCP 结果确定后，认证复核超时只能终止 Run，不能改写业务事实。"""

    async def scenario() -> None:
        definition = _mcp_definition()
        lease = _lease(definition=definition)
        repository = _Repository(lease.run)
        auth = _DeliveryAuth(block_after_request=3, block_seconds=5)
        mcp_executor = _DeliveryMcpExecutor(
            McpCallResult(
                mcp_status,
                mcp_code,
                "remote result",
                text="known external effect",
            ),
            business_delay_seconds=0.05,
            cleanup_delay_seconds=0.1,
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-real-runtime",
                        "test_tool",
                        {"query": "value"},
                    )
                )
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=_delivery_runtime(auth, mcp_executor),
            config=RunWorkerConfig(
                tool_timeout_seconds=0.3,
                tool_cleanup_seconds=0.15,
            ),
        )

        await executor.execute(lease)

        assert auth.requests == 3
        assert auth.post_tool_started.is_set()
        assert mcp_executor.calls == 1
        assert len(provider.requests) == 1
        assert repository.finish_tool_calls == 1
        assert repository.normal_finishes == 0
        assert repository.run.status is RunStatus.FAILED
        assert repository.run.safe_error_code == "run_auth_session_invalid"
        assert repository.last_finish_command is not None
        assert repository.last_finish_command.terminate_error_code == "run_auth_session_invalid"
        outcome = repository.last_finish_command.outcome
        assert outcome.status is expected_status
        assert outcome.code == mcp_code
        assert outcome.data["text"] == "known external effect"

    asyncio.run(scenario())


def test_real_runtime_uses_run_deadline_for_auth_after_tool_cleanup() -> None:
    """清理超出工具期限但 Run 仍有效时，正常交付并继续模型循环。"""

    async def scenario() -> None:
        definition = _mcp_definition()
        lease = _lease(definition=definition)
        repository = _Repository(lease.run)
        auth = _DeliveryAuth(block_after_request=10**6, block_seconds=5)
        mcp_executor = _DeliveryMcpExecutor(
            McpCallResult(
                McpCallStatus.OK,
                "mcp_tool_completed",
                "remote result",
                text="known external effect",
            ),
            business_delay_seconds=0.09,
            cleanup_delay_seconds=0.15,
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-slow-cleanup",
                        "test_tool",
                        {"query": "value"},
                    )
                ),
                _response(text="final answer"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=_delivery_runtime(auth, mcp_executor),
            config=RunWorkerConfig(
                tool_timeout_seconds=0.1,
                tool_cleanup_seconds=0.2,
            ),
        )

        await executor.execute(lease)

        assert auth.requests == 4
        assert not auth.post_tool_started.is_set()
        assert mcp_executor.calls == 1
        assert len(provider.requests) == 2
        assert repository.finish_tool_calls == 1
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED
        assert repository.run.safe_error_code is None
        assert repository.last_finish_command is not None
        assert repository.last_finish_command.terminate_error_code is None
        outcome = repository.last_finish_command.outcome
        assert outcome.status is ToolOutcomeStatus.OK
        assert outcome.code == "mcp_tool_completed"
        assert outcome.data["text"] == "known external effect"

    asyncio.run(scenario())


def test_real_mcp_cleanup_timeout_finishes_before_executor_watchdog() -> None:
    """真实 MCP 清理先收敛，外层守护不得抢先把已知结果改为 unknown。"""

    async def scenario() -> None:
        definition = _mcp_definition()
        lease = _lease(definition=definition)
        repository = _Repository(lease.run)
        auth = _DeliveryAuth(block_after_request=3, block_seconds=0.15)
        clients: list[_RealMcpSlowCloseClient] = []

        def client_factory(transport: Any, deadline_seconds: float) -> _RealMcpSlowCloseClient:
            del transport, deadline_seconds
            client = _RealMcpSlowCloseClient(business_delay_seconds=0.02)
            clients.append(client)
            return client

        mcp_executor = McpToolCallClient(
            client_factory=client_factory,
            transport_factory=lambda _: cast(Any, object()),
            cleanup_timeout_seconds=0.05,
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-cleanup-boundary",
                        "test_tool",
                        {"query": "value"},
                    )
                ),
                _response(text="final answer"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=_delivery_runtime(auth, mcp_executor),
            config=RunWorkerConfig(
                tool_timeout_seconds=0.08,
                tool_cleanup_seconds=0.05,
            ),
        )

        await executor.execute(lease)

        assert auth.requests == 4
        assert auth.post_tool_started.is_set()
        assert len(clients) == 1
        assert len(provider.requests) == 2
        assert repository.finish_tool_calls == 1
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED
        assert repository.run.safe_error_code is None
        assert repository.last_finish_command is not None
        assert repository.last_finish_command.terminate_error_code is None
        outcome = repository.last_finish_command.outcome
        assert outcome.status is ToolOutcomeStatus.OK
        assert outcome.code == "mcp_tool_completed"
        assert outcome.cleanup_error_code == "mcp_cleanup_failed"
        assert outcome.data["text"] == "known external effect"

    asyncio.run(scenario())


def test_unknown_tool_name_persists_call_and_error_result_before_continuing() -> None:
    """模型猜测的名称不能绕过快照，但仍留下成对调用与拒绝结果。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="must_not_run"))
        )
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-guessed",
                        "guessed_tool",
                        {"query": "value"},
                    )
                ),
                _response(text="The requested tool is unavailable."),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 2
        assert tool_runtime.calls == 0
        assert repository.begin_tool_calls == repository.finish_tool_calls == 1
        begin = repository.last_begin_command
        finish = repository.last_finish_command
        assert begin is not None and finish is not None
        assert begin.tool_id is None and begin.tool_kind is None
        call_part = next(
            part for part in begin.call_message.parts if isinstance(part, ToolCallPart)
        )
        result_part = next(
            part for part in finish.result_message.parts if isinstance(part, ToolResultPart)
        )
        assert call_part.name == "guessed_tool"
        assert call_part.tool_call_id == result_part.tool_call_id == "call-guessed"
        assert result_part.output is not None
        outcome = ToolOutcome.model_validate_json(result_part.output)
        assert outcome.code == "tool_unknown"
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_run_v1_cannot_execute_business_definition_from_an_unexpected_snapshot() -> None:
    """旧提示版本的 Run 即使快照异常，也不能执行任何业务工具。"""

    async def scenario() -> None:
        lease = _lease()
        lease = replace(
            lease,
            run=replace(lease.run, prompt_template_version="run-v1"),
        )
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="must_not_run"))
        )
        provider = _ScriptedProvider(
            [
                _response(call=LlmToolCall("call-v1", "test_tool", {"query": "value"})),
                _response(text="该工具不可用。"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert tool_runtime.calls == 0
        assert repository.begin_tool_calls == repository.finish_tool_calls == 1
        assert repository.last_begin_command is not None
        assert repository.last_begin_command.tool_id is None
        assert repository.last_finish_command is not None
        result_part = next(
            part
            for part in repository.last_finish_command.result_message.parts
            if isinstance(part, ToolResultPart)
        )
        assert result_part.output is not None
        assert ToolOutcome.model_validate_json(result_part.output).code == "tool_unknown"
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_penultimate_failed_request_uses_last_slot_as_final_answer() -> None:
    async def scenario() -> None:
        lease = _lease(used=38)
        repository = _Repository(lease.run)
        provider = _ScriptedProvider(
            [
                LlmProviderError("llm_rate_limited", retry_safe=True),
                _response(text="final answer"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 2
        assert provider.requests[0].tools
        assert provider.requests[0].tool_choice == "auto"
        assert provider.requests[1].tools == ()
        assert provider.requests[1].tool_choice == "none"
        assert repository.run.llm_request_count == 40
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_oversized_tool_arguments_fail_without_creating_a_placeholder_call() -> None:
    """执行层参数上限触发协议失败，不创建调用记录也不分发工具。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-oversized",
                        "test_tool",
                        {"query": "x" * 2000},
                    )
                )
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            config=RunWorkerConfig(tool_argument_bytes=1024),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert repository.begin_tool_calls == 0
        assert repository.finish_tool_calls == 0
        assert repository.failures == ["llm_tool_arguments_too_large"]
        assert repository.run.status is RunStatus.FAILED

    asyncio.run(scenario())
