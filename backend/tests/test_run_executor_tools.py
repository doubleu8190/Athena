"""Run Executor 的工具停止边界和最后额度语义。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp import types
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
    FakeSystemPromptRepository,
)

from app.run import executor as executor_module
from app.run.budget import RunLlmBudget
from app.run.compression import ContextPreparation
from app.run.config import RunWorkerConfig
from app.run.context import RunInputFileContext
from app.run.executor import RunExecutor as _RunExecutor
from app.run.executor import _ExecutionState
from app.tool.ports import RunToolRuntime
from app.tool.runtime import RunToolRuntimeService
from domain.auth.model import User, UserRole, UserStatus
from domain.file import PendingSessionFile
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
from domain.llm.model.compression_policy import CompressionPolicyVersion
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    StdioConfig,
)
from domain.message import (
    ArtifactFileRef,
    ConversationMessage,
    ConversationRole,
    ExecutionMessage,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    ContextStopReason,
    FailRunCommand,
    RecordRunUsageCommand,
    Run,
    RunContextSafetyCommitError,
    RunInputFiles,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.model.errors import RunStateError
from domain.run.ports import RunRepository
from domain.session.model import Session
from domain.skill import ActiveSkillGuide, RunSkillEntry, RunSkillPromptContext
from domain.tool import (
    AuthorizedRunContext,
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    BuiltinToolSource,
    FinishToolCallCommand,
    McpToolSource,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolBatchCall,
    RunToolCallRecord,
    ToolApprovalDecision,
    ToolCallPreparation,
    ToolCallStatus,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetryContext,
)
from infrastructure.mcp.execution import McpToolCallClient

NOW = datetime.now(UTC)


def RunExecutor(
    repository: RunRepository,
    profiles: ModelProfileRepository,
    provider: LlmStreamProvider,
    **kwargs: Any,
) -> _RunExecutor:
    """为单元测试注入固定正文，实际执行仍使用生产 RunExecutor。"""
    return _RunExecutor(
        repository,
        profiles,
        provider,
        system_prompts=FakeSystemPromptRepository(),
        **kwargs,
    )


def _runtime_profile(*, supports_parallel_tool_calls: bool = False) -> RuntimeModelProfile:
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id="profile-tools",
            name="tools",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="tools-model",
            has_api_key=True,
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
            supports_parallel_tool_calls=supports_parallel_tool_calls,
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


def _named_definition(model_name: str, *, requires_approval: bool = False) -> RuntimeToolDefinition:
    return RuntimeToolDefinition(
        tool_id=f"builtin_{model_name}",
        model_name=model_name,
        description=f"{model_name} tool",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source=BuiltinToolSource(version=1),
        requires_approval=requires_approval,
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
    input_file_ids: tuple[str, ...] = (),
) -> RunLease:
    # 全量测试运行时间可能超过固定模块时钟，每个用例都使用当前时刻避免任务刚创建就被判超时。
    now = datetime.now(UTC)
    run = Run(
        id="run-tools",
        session_id="session-tools",
        owner_id="owner-tools",
        status=RunStatus.RUNNING,
        trigger_message_id="message-tools",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile-tools",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=0,
        execution_epoch=1,
        worker_id="worker-tools",
        lease_expires_at=now + timedelta(minutes=2),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        tool_snapshot=_snapshot(definition),
        auth_session_id="auth-tools",
        llm_request_count=used,
        input_files=RunInputFiles(file_ids=input_file_ids),
    )
    return RunLease(run, run.lease_expires_at or now, run.execution_deadline_at or now)


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
        self,
        prompt_template_version: str,
        context: AuthorizedRunContext,
        *,
        depth: int = 0,
        allowed_tool_ids: Any = None,
        knowledge_base_ids: tuple[str, ...] | None = None,
    ) -> RuntimeToolSnapshot:
        del prompt_template_version, context, depth, allowed_tool_ids, knowledge_base_ids
        return _snapshot()

    def validate_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutcome | None:
        del model_name, arguments
        if definition is None:
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="tool_unknown",
                message="模型请求了冻结目录中不存在的工具。",
            )
        return None

    async def prepare_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        run: Any,
        context: AuthorizedRunContext | None,
        *,
        call_id: str,
    ) -> ToolCallPreparation:
        del run, context, call_id
        outcome = self.validate_call(definition, model_name, arguments)
        return ToolCallPreparation(
            requires_approval=False if definition is None else definition.requires_approval,
            outcome=outcome,
        )

    async def build_skill_context(
        self, run: Any, loaded_skill_ids: tuple[str, ...]
    ) -> RunSkillPromptContext:
        del run, loaded_skill_ids
        return RunSkillPromptContext()

    async def build_run_input_file_context(self, run: Any) -> tuple[Any, ...]:
        del run
        return ()

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
        self.records: dict[str, RunToolCallRecord] = {}
        self.batches: dict[str, RunToolBatch] = {}
        self.finish_tool_calls = 0
        self.finish_order: list[int] = []
        self.normal_finishes = 0
        self.failures: list[str] = []
        self.fail_commands: list[FailRunCommand] = []
        self.converge_calls: list[Any] = []

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        return True

    async def list_retry_context(self, *args: object) -> tuple[object, ...]:
        return ()

    async def list_context_messages(self, *args: object) -> tuple[object, ...]:
        return ()

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        del run_id
        return ()

    async def list_tool_calls(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_ordinal: int = 0,
        limit: int = 100,
    ) -> Any:
        del session_id, owner_id, run_id, after_ordinal, limit
        return None

    async def reserve_llm_request(self, command: Any) -> int:
        self.run = replace(self.run, llm_request_count=self.run.llm_request_count + 1)
        return self.run.llm_request_count

    async def record_usage(self, command: object) -> Run:
        return self.run

    async def begin_tool_call_batch(
        self, command: BeginToolCallBatchCommand
    ) -> BeginToolCallBatchResult:
        self.begin_tool_calls += 1
        self.last_batch_command = command
        call_parts = [part for part in command.call_message.parts if isinstance(part, ToolCallPart)]
        records: list[RunToolCallRecord] = []
        pending = False
        for ordinal, (item, _part) in enumerate(
            zip(command.calls, call_parts, strict=False), start=1
        ):
            outcome = item.immediate_outcome
            status = (
                ToolCallStatus.PENDING_APPROVAL
                if item.requires_approval and outcome is None
                else ToolCallStatus.APPROVED
            )
            result_id = None
            if outcome is not None:
                from domain.tool import persisted_status_for_outcome

                status = persisted_status_for_outcome(outcome)
                result_id = item.result_message.id if item.result_message is not None else None
                self.finish_tool_calls += 1
                if item.result_message is not None:
                    self.last_finish_command = FinishToolCallCommand(
                        run_id=command.run_id,
                        worker_id=command.worker_id,
                        execution_epoch=command.execution_epoch,
                        record_id=item.record_id,
                        result_message=item.result_message,
                        outcome=outcome,
                        finished_at=command.requested_at,
                    )
            pending = pending or status is ToolCallStatus.PENDING_APPROVAL
            record = RunToolCallRecord(
                id=item.record_id,
                run_id=command.run_id,
                session_id=command.call_message.session_id,
                tool_call_id=item.tool_call_id,
                ordinal=ordinal,
                tool_id=item.tool_id,
                tool_kind=item.tool_kind,
                model_name=item.model_name,
                execution_epoch=command.execution_epoch,
                status=status,
                call_message_id=command.call_message.id,
                result_message_id=result_id,
                started_at=None,
                finished_at=None,
                safe_error_code=None if outcome is None else outcome.code,
                outcome=outcome,
                requires_approval=item.requires_approval,
                requested_at=command.requested_at,
            )
            self.records[record.id] = record
            records.append(record)
            self.last_begin_command = type(
                "BeginView",
                (),
                {
                    "call_message": command.call_message,
                    "tool_id": item.tool_id,
                    "tool_kind": item.tool_kind,
                    "tool_call_id": item.tool_call_id,
                },
            )()
        if pending:
            self.run = replace(self.run, status=RunStatus.WAITING_FOR_APPROVAL)
        batch = RunToolBatch(
            call_message_id=command.call_message.id,
            thread_id=command.call_message.thread_id,
            round=command.round,
            calls=tuple(
                RunToolBatchCall(
                    record=record,
                    arguments=dict(
                        next(
                            part.arguments
                            for part in call_parts
                            if part.tool_call_id == record.tool_call_id
                        )
                    ),
                )
                for record in records
            ),
        )
        self.batches[batch.call_message_id] = batch
        return BeginToolCallBatchResult(batch, pending)

    async def get_resumable_tool_batch(self, run_id: str) -> RunToolBatch | None:
        del run_id
        for message_id, batch in self.batches.items():
            refreshed = replace(
                batch,
                calls=tuple(
                    replace(call, record=self.records[call.record.id]) for call in batch.calls
                ),
            )
            self.batches[message_id] = refreshed
            if any(call.record.status is ToolCallStatus.APPROVED for call in refreshed.calls):
                return refreshed
        return None

    async def admit_tool_call(self, command: Any) -> RunToolCallRecord:
        record = self.records[command.record_id]
        updated = replace(
            record,
            status=ToolCallStatus.EXECUTING,
            execution_epoch=command.execution_epoch,
            started_at=command.admitted_at,
        )
        self.records[record.id] = updated
        self.batches = {
            message_id: replace(
                batch,
                calls=tuple(
                    replace(call, record=updated) if call.record.id == updated.id else call
                    for call in batch.calls
                ),
            )
            for message_id, batch in self.batches.items()
        }
        return updated

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run:
        self.finish_tool_calls += 1
        self.last_finish_command = command
        record = self.records[command.record_id]
        self.finish_order.append(record.ordinal)
        updated = replace(
            record,
            status=(
                ToolCallStatus.UNKNOWN
                if command.outcome.status is ToolOutcomeStatus.UNKNOWN
                else (
                    ToolCallStatus.COMPLETED
                    if command.outcome.status is ToolOutcomeStatus.OK
                    else ToolCallStatus.FAILED
                )
            ),
            result_message_id=command.result_message.id,
            finished_at=command.finished_at,
            outcome=command.outcome,
            safe_error_code=(
                None if command.outcome.status is ToolOutcomeStatus.OK else command.outcome.code
            ),
        )
        self.records[record.id] = updated
        self.batches = {
            message_id: replace(
                batch,
                calls=tuple(
                    replace(call, record=updated) if call.record.id == updated.id else call
                    for call in batch.calls
                ),
            )
            for message_id, batch in self.batches.items()
        }
        if (
            command.outcome.status is ToolOutcomeStatus.UNKNOWN
            or command.terminate_error_code is not None
        ) and not command.defer_terminal_convergence:
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

    async def converge_tool_batch(self, command: Any) -> Run:
        self.converge_calls.append(command)
        self.run = replace(
            self.run,
            status=RunStatus.FAILED,
            safe_error_code=command.safe_error_code,
        )
        return self.run

    async def finish(self, command: object) -> Run:
        self.normal_finishes += 1
        self.run = replace(self.run, status=RunStatus.COMPLETED)
        return self.run

    async def fail(self, command: Any) -> Run:
        self.failures.append(command.safe_error_code)
        self.fail_commands.append(command)
        self.run = replace(self.run, status=RunStatus.FAILED)
        return self.run


class _Profiles:
    def __init__(
        self,
        *,
        supports_parallel_tool_calls: bool = False,
        compression_policy: CompressionPolicyVersion | None = None,
    ) -> None:
        self._supports_parallel_tool_calls = supports_parallel_tool_calls
        self._compression_policy = compression_policy

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile:
        return _runtime_profile(supports_parallel_tool_calls=self._supports_parallel_tool_calls)

    async def get_compression_policy(self, policy_id: str) -> CompressionPolicyVersion | None:
        del policy_id
        return self._compression_policy


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
            role=UserRole.MEMBER,
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
    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
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
        knowledge=None,
        project_knowledge=None,
    )


class _ScriptedProvider:
    def __init__(
        self,
        outcomes: list[LlmResponse | LlmProviderError],
        *,
        on_request: Callable[[int], None] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.requests: list[LlmRequest] = []
        self._on_request = on_request

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(len(self.requests) - 1)
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


def _multi_response(*calls: LlmToolCall, text: str = "") -> LlmResponse:
    output: tuple[LlmText | LlmToolCall, ...] = ((LlmText(text),) if text else ()) + calls
    return LlmResponse(
        output=output,
        status="completed",
        incomplete_reason=None,
        model="tools-model",
        response_id="resp-tools",
        request_id=None,
        usage=LlmUsage(1, 1, 2),
    )


class _ConcurrencyToolRuntime(_ToolRuntime):
    def __init__(self, *, delay_seconds: float = 0.02) -> None:
        super().__init__(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok")),
            delay_seconds=delay_seconds,
        )
        self.active = 0
        self.max_active = 0
        self.started_queries: list[str] = []

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
        del definition, model_name, context, deadline_seconds, run_remaining_seconds
        self.started_queries.append(str(arguments["query"]))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay_seconds)
            self.calls += 1
            return self.result
        finally:
            self.active -= 1


class _UnknownThenSlowToolRuntime(_ToolRuntime):
    def __init__(self) -> None:
        super().__init__(ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok")))
        self.executed_queries: list[str] = []

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
        del definition, model_name, context, deadline_seconds, run_remaining_seconds
        self.calls += 1
        self.executed_queries.append(str(arguments["query"]))
        if arguments["query"] == "1":
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_timeout",
                    message="unknown",
                )
            )
        await asyncio.sleep(0.05)
        return ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))


class _ValidatingToolRuntime(_ToolRuntime):
    def __init__(self) -> None:
        super().__init__(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="must_not_run"))
        )
        self.validate_calls = 0

    def validate_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutcome | None:
        del definition, arguments
        self.validate_calls += 1
        if model_name == "risky_tool":
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="tool_arguments_invalid",
                message="schema mismatch",
            )
        return None


class _InputFileRuntime(_ToolRuntime):
    """返回已授权的文件安全投影，用于断言真实 provider 请求。"""

    async def build_run_input_file_context(self, run: Run) -> tuple[RunInputFileContext, ...]:
        return tuple(
            RunInputFileContext(
                file_id=file_id,
                filename=f"{file_id}.txt",
                label=None,
                mime_type="text/plain",
                size_bytes=12,
                source="upload",
            )
            for file_id in run.input_files.file_ids
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


def test_run_input_file_projection_is_in_the_actual_provider_request() -> None:
    """提交与澄清累积的文件必须进入真实模型请求，而非只保存在 Run 行。"""

    async def scenario() -> None:
        lease = _lease(input_file_ids=("file_initial",))
        repository = _Repository(lease.run)
        tool_runtime = _InputFileRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="tool_completed"))
        )
        provider = _ScriptedProvider(
            [_response(text="initial done"), _response(text="answer done")]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
        )

        await executor.execute(lease)
        answer_lease = replace(
            lease,
            run=replace(
                lease.run,
                input_files=RunInputFiles(file_ids=("file_initial", "file_answer")),
            ),
        )
        await executor.execute(answer_lease)

        initial_request = "\n".join(message.content for message in provider.requests[0].messages)
        answer_request = "\n".join(message.content for message in provider.requests[1].messages)
        assert '"file_id":"file_initial"' in initial_request
        assert '"file_id":"file_answer"' not in initial_request
        assert '"file_id":"file_initial"' in answer_request
        assert '"file_id":"file_answer"' in answer_request
        assert '"filename":"file_answer.txt"' in answer_request
        assert "session-files/" not in answer_request

    asyncio.run(scenario())


def test_transient_provider_error_still_retries() -> None:
    """可恢复供应商错误在三次上限内继续自动重试。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = _ScriptedProvider(
            [
                LlmProviderError("llm_rate_limited", retry_safe=True),
                _response(text="retried answer"),
            ]
        )

        async def no_sleep(seconds: float) -> None:
            del seconds

        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            sleep=no_sleep,
        )

        await executor.execute(lease)

        assert len(provider.requests) == 2
        assert provider.requests[0] == provider.requests[1]
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_multiple_artifacts_publish_one_tool_result_and_one_chat_delivery() -> None:
    """双产物保留唯一 TOOL 消息，同时形成可分页恢复的普通助手文件消息。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        now = datetime.now(UTC)
        artifacts = (
            PendingSessionFile(
                id="file_report",
                filename="report.csv",
                label="report",
                mime_type="text/csv",
                size_bytes=10,
                sha256="a" * 64,
                object_key="session-artifacts/aa/key/report",
                output_index=0,
                created_at=now,
            ),
            PendingSessionFile(
                id="file_chart",
                filename="chart.png",
                label="chart",
                mime_type="image/png",
                size_bytes=20,
                sha256="b" * 64,
                object_key="session-artifacts/bb/key/chart",
                output_index=1,
                created_at=now,
            ),
        )
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(
                ToolOutcome(status=ToolOutcomeStatus.OK, code="tool_completed"),
                artifacts=artifacts,
            )
        )
        provider = _ScriptedProvider(
            [
                _response(call=LlmToolCall("call-artifacts", "test_tool", {"query": "x"})),
                _response(text="done"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
        )

        await executor.execute(lease)

        assert repository.last_finish_command is not None
        command = repository.last_finish_command
        tool_results = [
            part for part in command.result_message.parts if isinstance(part, ToolResultPart)
        ]
        assert len(tool_results) == 1
        assert [item.file_id for item in tool_results[0].artifacts] == [
            "file_report",
            "file_chart",
        ]
        assert command.delivery_message is not None
        assert command.delivery_message.role is ConversationRole.ASSISTANT
        assert [
            (part.file_id, part.filename)
            for part in command.delivery_message.parts
            if isinstance(part, FileRefPart)
        ] == [("file_report", "report.csv"), ("file_chart", "chart.png")]
        for part in command.delivery_message.parts:
            assert not hasattr(part, "object_key")

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


def test_real_runtime_continues_after_slow_mcp_cleanup() -> None:
    """清理超出工具业务期限但位于清理预算内时，正常交付并继续模型循环（无交付前冗余重鉴）。"""

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

        # 3 次鉴权分别为：首轮 LLM 前、MCP 工具执行前、次轮 LLM 前（不再执行交付前重鉴）。
        assert auth.requests == 3
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
        auth = _DeliveryAuth(block_after_request=10**6, block_seconds=0.15)
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

        assert auth.requests == 3
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


def test_unsupported_prompt_version_fails_before_provider_or_tool_calls() -> None:
    """不支持的模板版本必须在调用模型或工具前安全失败。"""

    async def scenario() -> None:
        lease = _lease()
        # 模拟历史 v1 Run：执行前不改数据，由执行器识别版本并安全失败。
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

        # 版本校验发生在准备阶段，模型与工具都不应被调用。
        assert provider.requests == []
        assert tool_runtime.calls == 0
        assert repository.failures == ["run_prompt_template_unsupported"]
        assert repository.run.status is RunStatus.FAILED
        assert repository.begin_tool_calls == 0
        assert repository.finish_tool_calls == 0

    asyncio.run(scenario())


def test_executor_uses_the_main_prompt_content_bound_to_the_run() -> None:
    """执行请求的首条 system 正文来自 Run 绑定版本，而不是代码常量。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = _ScriptedProvider([_response(text="custom prompt completed")])
        custom_prompt = "CUSTOM BOUND MAIN PROMPT"
        executor = _RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            system_prompts=FakeSystemPromptRepository(main_content=custom_prompt),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert provider.requests[0].messages[0].content == custom_prompt
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_missing_bound_prompt_fails_without_provider_or_content_fallback() -> None:
    """Run 引用缺失或类别损坏时安全失败，不回退到当前或代码内正文。"""

    async def scenario() -> None:
        lease = _lease()
        lease = replace(
            lease,
            run=replace(lease.run, main_prompt_version_id="sysp_missing"),
        )
        repository = _Repository(lease.run)
        provider = _ScriptedProvider([_response(text="must not run")])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
        )

        await executor.execute(lease)

        assert provider.requests == []
        assert repository.failures == ["system_prompt_unavailable"]
        assert repository.run.status is RunStatus.FAILED

    asyncio.run(scenario())


def test_oversized_bound_main_prompt_is_rejected_without_truncation() -> None:
    """主提示词在所有预算路径中计入窗口；超限时拒绝请求而不截断正文。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = _ScriptedProvider([_response(text="must not run")])
        oversized_prompt = "完整系统提示词不能截断" * 4_000
        executor = _RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            system_prompts=FakeSystemPromptRepository(main_content=oversized_prompt),
        )

        await executor.execute(lease)

        assert provider.requests == []
        assert repository.failures == ["run_context_safety_stop"]
        assert repository.run.status is RunStatus.FAILED
        command = repository.fail_commands[-1]
        assert command.context_stop_reason is ContextStopReason.HARD_WINDOW_EXCEEDED
        assert command.report_message is not None

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


def test_approval_barrier_holds_safe_calls_and_resume_does_not_replay_model() -> None:
    async def scenario() -> None:
        safe = _named_definition("test_tool")
        risky = _named_definition("risky_tool", requires_approval=True)
        lease = _lease()
        lease = replace(
            lease,
            run=replace(
                lease.run,
                tool_snapshot=RuntimeToolSnapshot(tools=(safe, risky)),
            ),
        )
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
        )
        provider = _ScriptedProvider(
            [
                _multi_response(
                    LlmToolCall("call-safe", "test_tool", {"query": "safe"}),
                    LlmToolCall("call-risky", "risky_tool", {"query": "risky"}),
                ),
                _multi_response(text="finished"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert tool_runtime.calls == 0
        assert repository.begin_tool_calls == 1
        assert repository.finish_tool_calls == 0
        assert repository.run.status is RunStatus.WAITING_FOR_APPROVAL
        risky_record = next(
            record for record in repository.records.values() if record.tool_call_id == "call-risky"
        )
        safe_record = next(
            record for record in repository.records.values() if record.tool_call_id == "call-safe"
        )
        assert risky_record.status is ToolCallStatus.PENDING_APPROVAL
        assert safe_record.status is ToolCallStatus.APPROVED

        repository.records[risky_record.id] = replace(
            risky_record,
            status=ToolCallStatus.APPROVED,
            approval_decision=ToolApprovalDecision.APPROVE,
            approval_decided_at=NOW,
        )
        # 恢复发生在场景实际运行时；不能使用模块导入时时钟，否则长套件会提前过期。
        resumed_at = datetime.now(UTC)
        repository.run = replace(
            repository.run,
            status=RunStatus.RUNNING,
            execution_epoch=2,
            worker_id="worker-resume",
            lease_expires_at=resumed_at + timedelta(minutes=2),
            execution_deadline_at=resumed_at + timedelta(minutes=1),
        )
        resumed_lease = RunLease(
            repository.run,
            resumed_at + timedelta(minutes=2),
            resumed_at + timedelta(minutes=1),
        )

        await executor.execute(resumed_lease)

        assert len(provider.requests) == 2
        assert tool_runtime.calls == 2
        assert repository.finish_tool_calls == 2
        assert repository.finish_order == [1, 2]
        assert repository.normal_finishes == 1
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_dynamic_skill_preparation_only_sends_valid_call_to_approval_barrier() -> None:
    """同批非法 Skill 即时失败，合法项仍按 Run 固定策略进入审批屏障。"""

    async def scenario() -> None:
        skill_definition = _named_definition("skill_execute")
        lease = _lease()
        lease = replace(
            lease,
            run=replace(
                lease.run,
                tool_snapshot=RuntimeToolSnapshot(tools=(skill_definition,)),
            ),
        )
        repository = _Repository(lease.run)

        class DynamicApprovalRuntime(_ToolRuntime):
            async def prepare_call(
                self,
                definition: RuntimeToolDefinition | None,
                model_name: str,
                arguments: dict[str, Any],
                run: Run,
                context: AuthorizedRunContext | None,
                *,
                call_id: str,
            ) -> ToolCallPreparation:
                del definition, model_name, run, context, call_id
                if arguments["skill_id"] == "invalid":
                    return ToolCallPreparation(
                        ToolOutcome(
                            status=ToolOutcomeStatus.ERROR,
                            code="skill_not_in_run_snapshot",
                            message="invalid skill",
                        ),
                        False,
                    )
                return ToolCallPreparation(None, True)

        tool_runtime = DynamicApprovalRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
        )
        provider = _ScriptedProvider(
            [
                _multi_response(
                    LlmToolCall(
                        "call-invalid-skill",
                        "skill_execute",
                        {"skill_id": "invalid"},
                    ),
                    LlmToolCall(
                        "call-valid-skill",
                        "skill_execute",
                        {"skill_id": "valid"},
                    ),
                )
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert tool_runtime.calls == 0
        assert repository.run.status is RunStatus.WAITING_FOR_APPROVAL
        records = {record.tool_call_id: record for record in repository.records.values()}
        assert records["call-invalid-skill"].status is ToolCallStatus.FAILED
        assert records["call-invalid-skill"].outcome is not None
        assert records["call-invalid-skill"].outcome.code == "skill_not_in_run_snapshot"
        assert records["call-valid-skill"].status is ToolCallStatus.PENDING_APPROVAL
        assert records["call-valid-skill"].requires_approval is True
        assert [item.requires_approval for item in repository.last_batch_command.calls] == [
            False,
            True,
        ]

    asyncio.run(scenario())


def test_parallel_batch_respects_concurrency_limit_and_stable_dispatch_order() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ConcurrencyToolRuntime()
        calls = tuple(
            LlmToolCall(f"call-{index}", "test_tool", {"query": str(index)})
            for index in range(1, 7)
        )
        provider = _ScriptedProvider([_multi_response(*calls), _multi_response(text="finished")])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(tool_batch_limit=8, tool_concurrency=3),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 2
        assert tool_runtime.started_queries == [str(index) for index in range(1, 7)]
        assert tool_runtime.max_active == 3
        assert repository.finish_tool_calls == 6
        assert sorted(repository.finish_order) == [1, 2, 3, 4, 5, 6]
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_unknown_after_start_stops_new_dispatch_and_converges_batch() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _UnknownThenSlowToolRuntime()
        calls = tuple(
            LlmToolCall(f"call-{index}", "test_tool", {"query": str(index)})
            for index in range(1, 5)
        )
        provider = _ScriptedProvider([_multi_response(*calls)])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(tool_batch_limit=8, tool_concurrency=2),
        )

        await executor.execute(lease)

        assert len(provider.requests) == 1
        assert 1 <= tool_runtime.calls <= 2
        assert "3" not in tool_runtime.executed_queries
        assert "4" not in tool_runtime.executed_queries
        assert repository.converge_calls
        assert repository.converge_calls[0].safe_error_code == "tool_outcome_unknown"
        assert repository.run.status is RunStatus.FAILED
        assert repository.run.safe_error_code == "tool_outcome_unknown"
        assert not repository.normal_finishes

    asyncio.run(scenario())


def test_persisted_batch_with_unknown_never_starts_a_new_model_request() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        unknown = RunToolCallRecord(
            id="record-unknown",
            run_id=lease.run.id,
            session_id=lease.run.session_id,
            tool_call_id="call-unknown",
            ordinal=1,
            tool_id="builtin_test_tool",
            tool_kind=ToolKind.BUILTIN,
            model_name="test_tool",
            execution_epoch=1,
            status=ToolCallStatus.UNKNOWN,
            call_message_id="exec-persisted-batch",
            result_message_id="exec-unknown-result",
            started_at=NOW,
            finished_at=NOW,
            safe_error_code="tool_timeout",
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.UNKNOWN,
                code="tool_timeout",
                message="unknown",
            ),
        )
        approved = replace(
            unknown,
            id="record-approved",
            tool_call_id="call-approved",
            ordinal=2,
            status=ToolCallStatus.APPROVED,
            result_message_id=None,
            started_at=None,
            finished_at=None,
            safe_error_code=None,
            outcome=None,
        )
        repository.records[unknown.id] = unknown
        repository.records[approved.id] = approved
        repository.batches["exec-persisted-batch"] = RunToolBatch(
            call_message_id="exec-persisted-batch",
            thread_id=lease.run.id,
            round=1,
            calls=(
                RunToolBatchCall(unknown, {"query": "unknown"}),
                RunToolBatchCall(approved, {"query": "approved"}),
            ),
        )
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="must_not_run"))
        )
        provider = _ScriptedProvider([])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert provider.requests == []
        assert tool_runtime.calls == 0
        assert repository.converge_calls
        assert repository.converge_calls[0].safe_error_code == "tool_outcome_unknown"
        assert repository.run.status is RunStatus.FAILED

    asyncio.run(scenario())


def test_parallel_capability_disabled_and_mixed_input_fail_before_batch() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
        )
        calls = (
            LlmToolCall("call-1", "test_tool", {"query": "one"}),
            LlmToolCall("call-2", "test_tool", {"query": "two"}),
        )
        disabled = _ScriptedProvider([_multi_response(*calls)])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, disabled),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )
        await executor.execute(lease)
        assert repository.failures == ["llm_parallel_tool_calls_unsupported"]
        assert repository.begin_tool_calls == 0
        assert tool_runtime.calls == 0

        mixed_repository = _Repository(lease.run)
        mixed = _ScriptedProvider(
            [
                _multi_response(
                    LlmToolCall("call-1", "request_user_input", {"question": "Which?"}),
                    LlmToolCall("call-2", "test_tool", {"query": "two"}),
                )
            ]
        )
        mixed_executor = RunExecutor(
            cast(RunRepository, mixed_repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, mixed),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )
        await mixed_executor.execute(lease)
        assert mixed_repository.failures == ["llm_tool_call_mix_unsupported"]
        assert mixed_repository.begin_tool_calls == 0
        assert tool_runtime.calls == 0

    asyncio.run(scenario())


def test_tool_batch_limit_rejects_nine_calls_without_dispatch() -> None:
    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        tool_runtime = _ToolRuntime(
            ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
        )
        calls = tuple(
            LlmToolCall(f"call-{index}", "test_tool", {"query": str(index)})
            for index in range(1, 10)
        )
        provider = _ScriptedProvider([_multi_response(*calls)])
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert repository.failures == ["llm_tool_batch_too_large"]
        assert repository.begin_tool_calls == 0
        assert tool_runtime.calls == 0

    asyncio.run(scenario())


def test_invalid_arguments_for_approval_tool_do_not_create_waiting_state() -> None:
    async def scenario() -> None:
        risky = _named_definition("risky_tool", requires_approval=True)
        lease = _lease()
        lease = replace(
            lease,
            run=replace(
                lease.run,
                tool_snapshot=RuntimeToolSnapshot(tools=(risky,)),
            ),
        )
        repository = _Repository(lease.run)
        tool_runtime = _ValidatingToolRuntime()
        provider = _ScriptedProvider(
            [
                _multi_response(LlmToolCall("call-risky", "risky_tool", {"query": "bad"})),
                _multi_response(text="handled"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(supports_parallel_tool_calls=True)),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, tool_runtime),
            config=RunWorkerConfig(),
        )

        await executor.execute(lease)

        assert tool_runtime.validate_calls == 1
        assert tool_runtime.calls == 0
        assert repository.finish_tool_calls == 1
        assert repository.run.status is RunStatus.COMPLETED
        assert all(record.status is ToolCallStatus.FAILED for record in repository.records.values())

    asyncio.run(scenario())


def _context_state(repository: _Repository) -> _ExecutionState:
    """构造只执行上下文预检的 RunExecutor 状态。"""
    lease = RunLease(
        repository.run,
        repository.run.lease_expires_at or NOW,
        repository.run.execution_deadline_at or NOW,
    )
    context = ToolExecutionContext(
        owner_id=repository.run.owner_id,
        chat_session_id=repository.run.session_id,
        project_id=None,
        run_id=repository.run.id,
        execution_epoch=repository.run.execution_epoch,
        auth_session_id=repository.run.auth_session_id or "",
        deadline_at=repository.run.execution_deadline_at or NOW,
    )
    return _ExecutionState(
        lease=lease,
        run=repository.run,
        loop_deadline=asyncio.get_running_loop().time() + 60,
        authorized=None,
        snapshot=repository.run.tool_snapshot or _snapshot(),
        main_prompt="main prompt",
        compression_prompt="compression prompt",
        budget=RunLlmBudget(
            cast(RunRepository, repository),
            context,
            used=repository.run.llm_request_count,
            maximum=40,
            worker_id=repository.run.worker_id or "",
        ),
        retry_context=(),
        input_files=(),
        event_cursor=repository.run.event_sequence,
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected_reason"),
    [
        (7373, 256, ContextStopReason.COMPRESSION_NOT_CONFIGURED),
        (7000, 2000, ContextStopReason.HARD_WINDOW_EXCEEDED),
    ],
)
def test_context_safety_gate_covers_final_only_without_compression_policy(
    monkeypatch: pytest.MonkeyPatch,
    input_tokens: int,
    output_tokens: int,
    expected_reason: ContextStopReason,
) -> None:
    """无策略和 final-only 都先按原业务 tools 做 90% 与硬窗口裁决。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        captured_tools: list[tuple[str, ...]] = []

        async def fake_measure(
            run: Run,
            messages: tuple[object, ...],
            profile: ModelProfileVersion,
            **kwargs: Any,
        ) -> tuple[tuple[object, ...], tuple[object, ...], int, int]:
            del run, messages, profile
            tools = kwargs["tools"]
            captured_tools.append(tuple(item.name for item in tools))
            return (), tuple(tools), input_tokens, output_tokens

        monkeypatch.setattr(executor_module, "measure_llm_request", fake_measure)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace()),
            tool_runtime=cast(
                RunToolRuntime,
                SimpleNamespace(
                    build_skill_context=AsyncMock(return_value=RunSkillPromptContext())
                ),
            ),
        )
        stop = AsyncMock()
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)
        state = _context_state(repository)

        prepared = await executor._prepare_request(
            state,
            _runtime_profile(),
            final_only=True,
        )

        assert prepared is None
        assert captured_tools == [("test_tool",)]
        stop.assert_awaited_once()
        stop_call = stop.await_args
        assert stop_call is not None
        assert stop_call.args[3] is expected_reason

    asyncio.run(scenario())


def test_hard_window_stop_preserves_compression_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """压缩输入过大与主请求硬窗同时发生时，报告必须保留两项停止事实。"""

    async def scenario() -> None:
        lease = _lease()
        policy = CompressionPolicyVersion(
            id="cmpol-hard-window",
            compression_model_profile_id="profile-tools",
            trigger_tokens=256_000,
            recent_ratio=0.2,
            recent_token_cap=32_768,
            summary_ratio=0.1,
            summary_token_cap=8_192,
            prompt_template_version="context-summary-v1",
            created_at=NOW,
        )
        run = replace(lease.run, compression_policy_version_id=policy.id)
        lease = RunLease(run, lease.lease_expires_at, lease.execution_deadline_at)
        repository = _Repository(run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(compression_policy=policy)),
            cast(LlmStreamProvider, SimpleNamespace()),
            tool_runtime=cast(
                RunToolRuntime,
                SimpleNamespace(
                    build_skill_context=AsyncMock(return_value=RunSkillPromptContext())
                ),
            ),
        )
        monkeypatch.setattr(
            executor,
            "_compressor",
            SimpleNamespace(
                prepare=AsyncMock(
                    return_value=ContextPreparation(
                        messages=(),
                        tools=(),
                        input_tokens=7000,
                        output_tokens=2000,
                        had_new_history=True,
                        stop_reason_hint=(ContextStopReason.COMPRESSION_INPUT_TOO_LARGE),
                    )
                )
            ),
        )
        stop = AsyncMock()
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)
        state = _context_state(repository)

        prepared = await executor._prepare_request(
            state,
            _runtime_profile(),
            final_only=True,
        )

        assert prepared is None
        stop_call = stop.await_args
        assert stop_call is not None
        assert stop_call.args[3] is ContextStopReason.HARD_WINDOW_EXCEEDED
        assert (
            stop_call.kwargs["compression_stop_reason"]
            is ContextStopReason.COMPRESSION_INPUT_TOO_LARGE
        )

        persisted_state = await executor._context_report_state(
            state,
            (),
            compression_stop_reason=ContextStopReason.COMPRESSION_INPUT_TOO_LARGE,
        )
        report = executor._system_context_report(
            ContextStopReason.HARD_WINDOW_EXCEEDED,
            persisted_state,
        )
        assert "完整输入与输出预留超过模型硬窗" in report
        assert "压缩输入无法一次装入模型窗口" in report
        assert "code: compression_input_too_large" in report
        assert (
            "- 压缩尝试结论：压缩输入无法一次装入模型窗口（code: compression_input_too_large）。"
            in report
        )
        assert all(
            heading in report
            for heading in (
                "上下文安全停止。任务未标记为完成。",
                "已完成：",
                "产出：",
                "未完成：",
                "停止原因：",
            )
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("context_window_tokens", "input_tokens", "output_tokens"),
    [
        (8192, 7372, 256),
        (10_000, 8000, 2000),
    ],
)
def test_context_safety_gate_allows_below_ninety_and_exact_hard_window(
    monkeypatch: pytest.MonkeyPatch,
    context_window_tokens: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """完整输入低于 90% 且 I+O 恰好等于 W 时允许业务调用。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)

        async def fake_measure(
            run: Run,
            messages: tuple[object, ...],
            profile: ModelProfileVersion,
            **kwargs: Any,
        ) -> tuple[tuple[object, ...], tuple[object, ...], int, int]:
            del run, messages, profile
            return (), tuple(kwargs["tools"]), input_tokens, output_tokens

        monkeypatch.setattr(executor_module, "measure_llm_request", fake_measure)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace()),
        )
        stop = AsyncMock()
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)
        runtime = replace(
            _runtime_profile(),
            version=replace(
                _runtime_profile().version,
                context_window_tokens=context_window_tokens,
            ),
        )

        prepared = await executor._prepare_request(
            _context_state(repository),
            runtime,
            final_only=True,
        )

        assert prepared is not None
        assert prepared.input_tokens == input_tokens
        assert prepared.output_tokens == output_tokens
        assert prepared.tools == ()
        stop.assert_not_awaited()

    asyncio.run(scenario())


def test_resumable_batch_is_blocked_before_side_effect_at_safety_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待恢复工具批次在主循环中先经上下文闸门，命中后不再执行。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        repository.get_resumable_tool_batch = AsyncMock(return_value=object())  # type: ignore[method-assign]

        async def fake_measure(
            run: Run,
            messages: tuple[object, ...],
            profile: ModelProfileVersion,
            **kwargs: Any,
        ) -> tuple[tuple[object, ...], tuple[object, ...], int, int]:
            del run, messages, profile
            return (), tuple(kwargs["tools"]), 7373, 256

        monkeypatch.setattr(executor_module, "measure_llm_request", fake_measure)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace()),
            tool_runtime=cast(
                RunToolRuntime,
                SimpleNamespace(
                    build_skill_context=AsyncMock(return_value=RunSkillPromptContext())
                ),
            ),
        )
        stop = AsyncMock()
        execute_batch = AsyncMock(return_value=True)
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)
        monkeypatch.setattr(executor, "_execute_persisted_batch", execute_batch)

        await executor._run_loop(_context_state(repository))

        stop.assert_awaited_once()
        execute_batch.assert_not_awaited()

    asyncio.run(scenario())


def test_context_safety_stop_records_auth_failure_when_authorization_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前置鉴权失效时直接走普通鉴权失败，不调用模型也不写入报告。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        authorization = SimpleNamespace(authorize=AsyncMock(return_value=None))
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, authorization),
        )
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        await executor.execute(lease)

        assert authorization.authorize.await_count == 1
        assert provider.complete.await_count == 0
        assert repository.failures == ["run_auth_session_invalid"]
        command = repository.fail_commands[-1]
        assert command.report_message is None
        assert command.context_stop_reason is None
        assert command.usage.attempts == ()

    asyncio.run(scenario())


def test_context_safety_stop_aborts_commit_when_authorization_lost_before_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前置鉴权通过但提交前二次鉴权丢失时，抛出 authorization_lost 且不写报告。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        authorization = SimpleNamespace(
            authorize=AsyncMock(
                side_effect=[
                    AuthorizedRunContext(
                        lease.run.owner_id,
                        lease.run.session_id,
                        None,
                        lease.run.auth_session_id or "",
                    ),
                    None,
                ]
            )
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(RunToolRuntime, authorization),
            id_factory=lambda prefix: f"{prefix}-auth-lost",
        )
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        with pytest.raises(RunContextSafetyCommitError) as raised:
            await executor.execute(lease)

        assert raised.value.report_message_id == "msg-auth-lost"
        assert raised.value.reason == "authorization_lost"
        assert authorization.authorize.await_count == 2
        assert provider.complete.await_count == 0
        assert repository.fail_commands == []
        assert repository.failures == []

    asyncio.run(scenario())


def test_context_safety_stop_persists_deterministic_system_report_without_calling_provider() -> (
    None
):
    """安全停止直接落库确定性四段系统报告，不调用 provider.complete 也不消耗模型额度。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock(side_effect=LlmProviderError("llm_timeout")))
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            id_factory=lambda prefix: f"{prefix}-fixed",
        )
        state = _context_state(repository)

        await executor._stop_for_context_safety(
            state,
            _runtime_profile(),
            (),
            ContextStopReason.NO_COMPRESSIBLE_HISTORY,
        )

        assert provider.complete.await_count == 0
        assert state.budget.used == 0
        assert repository.failures == ["run_context_safety_stop"]
        command = repository.fail_commands[-1]
        assert command.context_stop_reason is ContextStopReason.NO_COMPRESSIBLE_HISTORY
        assert command.usage.attempts == ()
        assert command.report_message is not None
        report_text = command.report_message.parts[0]
        assert isinstance(report_text, TextPart)
        assert all(
            heading in report_text.text
            for heading in (
                "上下文安全停止。任务未标记为完成。",
                "已完成：",
                "产出：",
                "未完成：",
                "停止原因：",
            )
        )
        assert command.report_message.metadata.provider == "system_context_safety"
        assert command.report_message.metadata.model is None

    asyncio.run(scenario())


def test_context_safety_gate_covers_exhausted_compression_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只剩最终槽位时仍先按原业务工具测量，并按预算耗尽原因安全停机。"""

    async def scenario() -> None:
        lease = _lease(used=39)
        run = replace(lease.run, compression_policy_version_id="cmpol-no-slot")
        lease = RunLease(
            run,
            lease.lease_expires_at,
            lease.execution_deadline_at,
        )
        repository = _Repository(run)
        policy = CompressionPolicyVersion(
            id="cmpol-no-slot",
            compression_model_profile_id="profile-tools",
            trigger_tokens=262_144,
            recent_ratio=0.2,
            recent_token_cap=32_768,
            summary_ratio=0.1,
            summary_token_cap=8_192,
            prompt_template_version="context-summary-v1",
            created_at=NOW,
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(compression_policy=policy)),
            cast(LlmStreamProvider, SimpleNamespace()),
        )
        prepare = AsyncMock(
            return_value=ContextPreparation(
                messages=(),
                tools=(),
                input_tokens=7373,
                output_tokens=256,
                had_new_history=True,
                stop_reason_hint=ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED,
            )
        )
        monkeypatch.setattr(
            executor,
            "_compressor",
            SimpleNamespace(prepare=prepare),
        )
        stop = AsyncMock()
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)

        prepared = await executor._prepare_request(
            _context_state(repository),
            _runtime_profile(),
            final_only=True,
        )

        assert prepared is None
        prepare.assert_awaited_once()
        assert prepare.await_args is not None
        assert prepare.await_args.kwargs["allow_compression"] is False
        stop.assert_awaited_once()
        stop_call = stop.await_args
        assert stop_call is not None
        assert stop_call.args[3] is ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED

    asyncio.run(scenario())


def test_oversized_existing_summary_without_new_history_stops_at_safety_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧摘要即使超过新 cap 也照常计入完整输入，达到 90% 后安全停机。"""

    async def scenario() -> None:
        lease = _lease()
        run = replace(lease.run, compression_policy_version_id="cmpol-old-summary")
        lease = RunLease(
            run,
            lease.lease_expires_at,
            lease.execution_deadline_at,
        )
        repository = _Repository(run)
        policy = CompressionPolicyVersion(
            id="cmpol-old-summary",
            compression_model_profile_id="profile-tools",
            trigger_tokens=262_144,
            recent_ratio=0.2,
            recent_token_cap=32_768,
            summary_ratio=0.1,
            summary_token_cap=8_192,
            prompt_template_version="context-summary-v1",
            created_at=NOW,
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles(compression_policy=policy)),
            cast(LlmStreamProvider, SimpleNamespace()),
        )
        prepare = AsyncMock(
            return_value=ContextPreparation(
                messages=(),
                tools=(),
                input_tokens=7373,
                output_tokens=256,
                had_new_history=False,
                stop_reason_hint=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )
        )
        monkeypatch.setattr(
            executor,
            "_compressor",
            SimpleNamespace(prepare=prepare),
        )
        stop = AsyncMock()
        monkeypatch.setattr(executor, "_stop_for_context_safety", stop)

        prepared = await executor._prepare_request(
            _context_state(repository),
            _runtime_profile(),
            final_only=False,
        )

        assert prepared is None
        stop.assert_awaited_once()
        stop_call = stop.await_args
        assert stop_call is not None
        assert stop_call.args[3] is ContextStopReason.NO_COMPRESSIBLE_HISTORY

    asyncio.run(scenario())


def test_system_context_report_renders_all_stop_reasons_and_commit_guard_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖全部 8 种 ContextStopReason 文案与关键安全提交错误原因码。"""
    from app.run.safe_stop import CONTEXT_STOP_REASON_TEXT

    assert set(CONTEXT_STOP_REASON_TEXT) == set(ContextStopReason)
    for reason, expected_text in CONTEXT_STOP_REASON_TEXT.items():
        rendered = _RunExecutor._system_context_report(reason, {})
        assert "上下文安全停止。任务未标记为完成。" in rendered
        assert all(label in rendered for label in ("已完成：", "产出：", "未完成：", "停止原因："))
        assert expected_text in rendered

    async def scenario() -> None:
        # 1. worker_owner_missing
        lease = _lease()
        no_worker_run = replace(lease.run, worker_id=None)
        repository = _Repository(no_worker_run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace(complete=AsyncMock())),
        )
        with pytest.raises(RunContextSafetyCommitError) as missing_worker:
            await executor._stop_for_context_safety(
                _context_state(repository),
                _runtime_profile(),
                (),
                ContextStopReason.HARD_WINDOW_EXCEEDED,
            )
        assert missing_worker.value.reason == "worker_owner_missing"
        assert missing_worker.value.report_message_id is None

        # 2. liveness_check_failed
        live_repo = _Repository(_lease().run)
        monkeypatch.setattr(
            live_repo,
            "is_live_execution",
            AsyncMock(side_effect=RuntimeError("db_down")),
        )
        executor2 = RunExecutor(
            cast(RunRepository, live_repo),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace(complete=AsyncMock())),
        )
        with pytest.raises(RunContextSafetyCommitError) as live_err:
            await executor2._stop_for_context_safety(
                _context_state(live_repo),
                _runtime_profile(),
                (),
                ContextStopReason.HARD_WINDOW_EXCEEDED,
            )
        assert live_err.value.reason == "liveness_check_failed"

        # 3. authorization_check_failed
        auth_repo = _Repository(_lease().run)
        executor3 = RunExecutor(
            cast(RunRepository, auth_repo),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace(complete=AsyncMock())),
            tool_runtime=cast(
                RunToolRuntime,
                SimpleNamespace(authorize=AsyncMock(side_effect=RuntimeError("auth_down"))),
            ),
        )
        with pytest.raises(RunContextSafetyCommitError) as auth_err:
            await executor3._stop_for_context_safety(
                _context_state(auth_repo),
                _runtime_profile(),
                (),
                ContextStopReason.HARD_WINDOW_EXCEEDED,
            )
        assert auth_err.value.reason == "authorization_check_failed"

        # 4. deadline_expired
        expired_repo = _Repository(_lease().run)
        executor4 = RunExecutor(
            cast(RunRepository, expired_repo),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace(complete=AsyncMock())),
            id_factory=lambda prefix: f"{prefix}-expired",
        )
        expired_state = replace(_context_state(expired_repo), loop_deadline=0.0)
        with pytest.raises(RunContextSafetyCommitError) as deadline_err:
            await executor4._stop_for_context_safety(
                expired_state,
                _runtime_profile(),
                (),
                ContextStopReason.HARD_WINDOW_EXCEEDED,
            )
        assert deadline_err.value.reason == "deadline_expired"
        assert deadline_err.value.report_message_id == "msg-expired"

    asyncio.run(scenario())


def test_context_report_state_preserves_metadata_and_redacts_sensitive_payloads() -> None:
    """报告状态保留当前 Run 文件、Skill 标识、重试与产物元数据，并彻底脱敏正文与工具原始输出。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, SimpleNamespace()),
        )
        skill_context = RunSkillPromptContext(
            catalog=(
                RunSkillEntry(
                    skill_id="skill_finance",
                    slug="finance",
                    name="财报分析",
                    description="分析财报",
                    version_id="skillver_1",
                    version_label="v1",
                    requires_approval=True,
                ),
            ),
            active=(
                ActiveSkillGuide(
                    skill_id="skill_finance",
                    name="财报分析",
                    version_id="skillver_1",
                    version_label="v1",
                    guide="完整技能指南正文",
                ),
            ),
        )
        conversation = ConversationMessage(
            id=lease.run.trigger_message_id,
            session_id=lease.run.session_id,
            role=ConversationRole.USER,
            status=MessageStatus.COMPLETED,
            sequence=1,
            parts=(
                TextPart("完整用户请求"),
                FileRefPart("file_message", "request.pdf", "application/pdf", 21),
            ),
            reply_to_message_id=None,
            run_id=lease.run.id,
            client_message_id=None,
            created_at=NOW,
            completed_at=NOW,
            metadata=MessageMetadata(),
        )
        execution = ExecutionMessage(
            id="execution_result",
            session_id=lease.run.session_id,
            run_id=lease.run.id,
            thread_id=lease.run.trigger_message_id,
            sequence=2,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id="call_result",
                    status=ToolResultStatus.SUCCESS,
                    output="完整工具输出，包含关键事实",
                    artifacts=(
                        ArtifactFileRef(
                            file_id="file_artifact",
                            filename="result.csv",
                            mime_type="text/csv",
                            size_bytes=33,
                            label="结果",
                        ),
                    ),
                ),
            ),
            tool_call_id="call_result",
            source_message_id="execution_call",
            created_at=NOW,
            metadata=MessageMetadata(),
        )
        state = replace(
            _context_state(repository),
            retry_context=(
                ToolRetryContext(
                    run_id="run_source",
                    tool_name="lookup",
                    safe_error_code="tool_failed",
                ),
            ),
            input_files=(
                RunInputFileContext(
                    file_id="file_input",
                    filename="input.xlsx",
                    label="输入",
                    mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                    size_bytes=44,
                    source="session",
                ),
            ),
        )

        report_state = await executor._context_report_state(
            state,
            (conversation, execution),
            skill_context=skill_context,
        )

        assert report_state["message_file_references"] == [
            {
                "type": "file_ref",
                "file_id": "file_message",
                "filename": "request.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 21,
            }
        ]
        assert cast(list[dict[str, object]], report_state["input_files"])[0]["file_id"] == (
            "file_input"
        )
        loaded_skills = cast(dict[str, object], report_state["loaded_skills"])
        active = cast(list[dict[str, object]], loaded_skills["active"])
        assert active == [
            {
                "skill_id": "skill_finance",
                "name": "财报分析",
                "version_id": "skillver_1",
                "version_label": "v1",
            }
        ]
        assert "guide" not in active[0]
        assert cast(list[dict[str, object]], report_state["retry_context"])[0] == {
            "run_id": "run_source",
            "tool_name": "lookup",
            "safe_error_code": "tool_failed",
        }
        assert len(cast(list[dict[str, object]], report_state["conversation"])) == 1
        assert len(cast(list[dict[str, object]], report_state["execution"])) == 1
        assert cast(list[dict[str, object]], report_state["artifacts"])[0]["file_id"] == (
            "file_artifact"
        )
        serialized_state = json.dumps(report_state, ensure_ascii=False)
        assert "完整技能指南正文" not in serialized_state
        assert "完整工具输出，包含关键事实" not in serialized_state
        assert "完整用户请求" not in serialized_state

        system_report = _RunExecutor._system_context_report(
            ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            report_state,
        )
        assert "file_message" in system_report
        assert "file_input" in system_report
        assert "skill_finance" in system_report
        assert "run_source" in system_report
        assert "file_artifact" in system_report
        assert "完整技能指南正文" not in system_report
        assert "完整工具输出，包含关键事实" not in system_report

    asyncio.run(scenario())


def test_execute_replays_same_context_report_command_after_uncertain_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告首事务抛错后，execute 用同一完整命令重放且全程不调用模型。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            id_factory=lambda prefix: f"{prefix}-replayed-report",
        )
        original_fail = repository.fail
        attempted_commands: list[FailRunCommand] = []

        async def flaky_fail(command: FailRunCommand) -> Run:
            attempted_commands.append(command)
            if len(attempted_commands) == 1:
                raise RuntimeError("transaction_result_uncertain")
            return await original_fail(command)

        usage_writes = AsyncMock()
        monkeypatch.setattr(repository, "fail", flaky_fail)
        monkeypatch.setattr(repository, "record_usage", usage_writes)
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        await executor.execute(lease)

        assert provider.complete.await_count == 0
        assert len(attempted_commands) == 2
        assert attempted_commands[1] is attempted_commands[0]
        assert repository.failures == ["run_context_safety_stop"]
        assert len(repository.fail_commands) == 1
        command = repository.fail_commands[0]
        assert command.report_message is not None
        assert command.report_message.id == "msg-replayed-report"
        assert command.report_message.metadata.provider == "system_context_safety"
        assert command.report_message.metadata.model is None
        assert command.context_stop_reason is ContextStopReason.NO_COMPRESSIBLE_HISTORY
        assert command.usage.attempts == ()
        usage_writes.assert_not_awaited()

    asyncio.run(scenario())


def test_execute_propagates_context_safety_commit_error_without_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持续报告事务失败穿透 execute，不调用模型也不写入无报告普通失败。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            id_factory=lambda prefix: f"{prefix}-lost-report",
        )
        attempted_commands: list[FailRunCommand] = []

        async def permanently_failed(command: FailRunCommand) -> Run:
            attempted_commands.append(command)
            raise RuntimeError("report_transaction_unavailable")

        usage_commands: list[RecordRunUsageCommand] = []

        async def record_usage(command: RecordRunUsageCommand) -> Run:
            usage_commands.append(command)
            return repository.run

        monkeypatch.setattr(
            repository,
            "fail",
            permanently_failed,
        )
        monkeypatch.setattr(repository, "record_usage", record_usage)
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.COMPRESSION_FAILED,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        with pytest.raises(RunContextSafetyCommitError) as raised:
            await executor.execute(lease)

        assert raised.value.report_message_id == "msg-lost-report"
        assert raised.value.reason == "commit_failed"
        assert provider.complete.await_count == 0
        assert len(attempted_commands) == 3
        assert all(command is attempted_commands[0] for command in attempted_commands)
        assert attempted_commands[0].report_message is not None
        assert repository.failures == []
        assert repository.fail_commands == []
        assert usage_commands == []

    asyncio.run(scenario())


def test_execute_does_not_retry_or_generic_fail_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交前失租时不调用模型、不重放报告也不写普通失败。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
        )
        live_checks = 0

        async def lose_lease_before_commit(*_: object) -> bool:
            nonlocal live_checks
            live_checks += 1
            return live_checks == 1

        usage_commands: list[RecordRunUsageCommand] = []

        async def record_usage(command: RecordRunUsageCommand) -> Run:
            usage_commands.append(command)
            return repository.run

        monkeypatch.setattr(repository, "is_live_execution", lose_lease_before_commit)
        monkeypatch.setattr(repository, "record_usage", record_usage)
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.COMPRESSION_INSUFFICIENT,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        with pytest.raises(RunContextSafetyCommitError) as raised:
            await executor.execute(lease)

        assert raised.value.reason == "lease_lost"
        assert provider.complete.await_count == 0
        assert repository.fail_commands == []
        assert repository.failures == []
        assert usage_commands == []

    asyncio.run(scenario())


def test_execute_stops_context_report_replay_when_repository_loses_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓储在提交时发现失租后立即停止重放，不调用模型也不写普通失败。"""

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        provider = SimpleNamespace(complete=AsyncMock())
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
        )
        failed = AsyncMock(side_effect=RunStateError("run_execution_lease_lost"))
        usage_commands: list[RecordRunUsageCommand] = []

        async def record_usage(command: RecordRunUsageCommand) -> Run:
            usage_commands.append(command)
            return repository.run

        monkeypatch.setattr(repository, "fail", failed)
        monkeypatch.setattr(repository, "record_usage", record_usage)
        state = _context_state(repository)

        async def forced_context_stop(_: _ExecutionState) -> None:
            await executor._stop_for_context_safety(
                state,
                _runtime_profile(),
                (),
                ContextStopReason.COMPRESSION_FAILED,
            )

        monkeypatch.setattr(executor, "_prepare", AsyncMock(return_value=state))
        monkeypatch.setattr(executor, "_run_loop", forced_context_stop)

        with pytest.raises(RunContextSafetyCommitError) as raised:
            await executor.execute(lease)

        assert raised.value.reason == "command_rejected"
        assert failed.await_count == 1
        assert provider.complete.await_count == 0
        assert usage_commands == []
        assert repository.failures == []

    asyncio.run(scenario())


@pytest.mark.parametrize("abandon_raises", [False, True])
def test_executor_abandons_artifacts_and_converges_when_session_file_quota_exceeded(
    abandon_raises: bool,
) -> None:
    """finish_tool_call 因会话文件配额超限回滚时，主动清理临时产物并收敛工具调用与脚本审计。"""
    from domain.file import PendingSessionFile
    from domain.skill import SkillExecutionCompletion, SkillExecutionStatus

    async def scenario() -> None:
        artifact_1 = PendingSessionFile(
            id="file_quota_1",
            filename="out1.txt",
            label="输出 1",
            mime_type="text/plain",
            size_bytes=12,
            sha256="a" * 64,
            object_key="session-artifacts/aa/call/file_quota_1",
            output_index=0,
            created_at=NOW,
        )
        artifact_2 = PendingSessionFile(
            id="file_quota_2",
            filename="out2.txt",
            label="输出 2",
            mime_type="text/plain",
            size_bytes=24,
            sha256="b" * 64,
            object_key="session-artifacts/aa/call/file_quota_2",
            output_index=1,
            created_at=NOW,
        )
        completion = SkillExecutionCompletion(
            execution_id="skexec_quota_1",
            status=SkillExecutionStatus.SUCCEEDED,
            log_object_key="skill-logs/aa/call/skexec_quota_1.log",
            log_truncated=False,
            log_error_code=None,
            error_code=None,
            cleanup_error_code=None,
            finished_at=NOW,
        )
        abandoned: list[tuple[PendingSessionFile, ...]] = []

        class _ArtifactRuntime(_ToolRuntime):
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
                del (
                    definition,
                    model_name,
                    arguments,
                    context,
                    deadline_seconds,
                    run_remaining_seconds,
                )
                return ToolExecutionResult(
                    outcome=ToolOutcome(
                        status=ToolOutcomeStatus.OK,
                        code="skill_execution_succeeded",
                        message="脚本执行成功。",
                        data={"artifact_count": 2},
                    ),
                    artifacts=(artifact_1, artifact_2),
                    skill_execution_completion=completion,
                )

            async def abandon_artifacts(
                self,
                artifacts: tuple[PendingSessionFile, ...],
            ) -> None:
                abandoned.append(artifacts)
                if abandon_raises:
                    raise RuntimeError("minio_delete_transient_error")

        class _QuotaRejectingRepository(_Repository):
            def __init__(self, run: Run) -> None:
                super().__init__(run)
                self.finish_attempts: list[FinishToolCallCommand] = []

            async def finish_tool_call(self, command: FinishToolCallCommand) -> Run:
                self.finish_attempts.append(command)
                if command.artifacts:
                    raise ValueError("session_file_quota_exceeded")
                return await super().finish_tool_call(command)

        lease = _lease()
        repository = _QuotaRejectingRepository(lease.run)
        provider = _ScriptedProvider(
            [
                _response(
                    call=LlmToolCall(
                        "call-quota-1",
                        "test_tool",
                        {"query": "generate"},
                    )
                ),
                _response(text="文件数量超限，已停止保存产物。"),
            ]
        )
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, provider),
            tool_runtime=cast(
                RunToolRuntime,
                _ArtifactRuntime(
                    ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
                ),
            ),
        )

        await executor.execute(lease)

        assert abandoned == [(artifact_1, artifact_2)]
        assert len(repository.finish_attempts) == 2
        assert repository.finish_attempts[0].artifacts == (artifact_1, artifact_2)
        converged = repository.finish_attempts[1]
        assert converged.outcome.status is ToolOutcomeStatus.ERROR
        assert converged.outcome.code == "session_file_quota_exceeded"
        assert converged.artifacts == ()
        assert converged.delivery_message is None
        assert converged.skill_execution_completion is not None
        assert converged.skill_execution_completion.execution_id == "skexec_quota_1"
        assert converged.skill_execution_completion.status is SkillExecutionStatus.FAILED
        assert converged.skill_execution_completion.error_code == "session_file_quota_exceeded"
        assert converged.skill_execution_completion.log_object_key == (
            "skill-logs/aa/call/skexec_quota_1.log"
        )
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_executor_text_delta_batching_fast_pushes_first_and_flushes_by_bytes_time_and_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TextDelta 首包零延迟快推，后续按 256B / 40ms 合批，遇工具/完成事件与流结束强制 flush。"""
    from domain.llm.model import TextDelta, ToolCallStarted
    from domain.run.ports import RunEventTransport

    now_clock = [100.0]
    monkeypatch.setattr("app.run.executor.time.monotonic", lambda: now_clock[0])

    published: list[tuple[str, int, str, int]] = []
    call_log: list[str] = []

    class RecordingPublisher:
        async def publish_text(
            self,
            run_id: str,
            execution_epoch: int,
            text: str,
            *,
            round: int = 0,
        ) -> None:
            published.append((run_id, execution_epoch, text, round))
            call_log.append(f"publish:{text}")

        async def publish_persisted(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    class StreamingProvider:
        def __init__(self) -> None:
            self.calls = 0

        @asynccontextmanager
        async def stream(
            self, runtime: RuntimeModelProfile, request: LlmRequest
        ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
            del runtime, request
            self.calls += 1
            call_no = self.calls

            async def _events() -> AsyncIterator[LlmStreamEvent]:
                if call_no == 1:
                    # 1. 先来空 TextDelta（置 emitted_output，但不消费首包快推名额）
                    yield TextDelta(0, 0, "")
                    # 2. 首条非空 TextDelta 立即快推（TTFT 零额外延迟）
                    yield TextDelta(0, 0, "首包")
                    assert published == [("run-tools", 1, "首包", 1)]
                    # 3. 连续短分片在 40ms 和 256B 之内累积缓冲
                    now_clock[0] += 0.01
                    yield TextDelta(0, 0, "a")
                    now_clock[0] += 0.01
                    yield TextDelta(0, 0, "b")
                    assert len(published) == 1
                    # 4. 超过 40ms 时间窗触发合批 flush（"ab" + "c"）
                    now_clock[0] += 0.035
                    yield TextDelta(0, 0, "c")
                    assert len(published) == 2
                    assert published[1][2] == "abc"
                    # 5. 多字节中文达到 >= 256 UTF-8 字节立即触发合批 flush
                    now_clock[0] += 0.005
                    yield TextDelta(0, 0, "中" * 50)  # 150B < 256B
                    assert len(published) == 2
                    now_clock[0] += 0.005
                    yield TextDelta(0, 0, "文" * 40)  # +120B = 270B >= 256B
                    assert len(published) == 3
                    assert published[2][2] == ("中" * 50) + ("文" * 40)
                    # 6. 尾部短文本在 ToolCallStarted 前强制 flush
                    now_clock[0] += 0.005
                    yield TextDelta(0, 0, "tail-before-tool")
                    assert len(published) == 3
                    call_log.append("yield:tool_start")
                    yield ToolCallStarted(output_index=0, call_id="call-batch-1", name="test_tool")
                    yield ResponseCompleted(
                        _response(
                            text="首包abc" + ("中" * 50) + ("文" * 40) + "tail-before-tool",
                            call=LlmToolCall("call-batch-1", "test_tool", {"query": "q"}),
                        )
                    )
                else:
                    # 第 2 轮：首包快推 + 短尾片在 ResponseCompleted 前强制 flush
                    yield TextDelta(0, 0, "二轮首")
                    now_clock[0] += 0.005
                    yield TextDelta(0, 0, "尾1")
                    now_clock[0] += 0.005
                    yield TextDelta(0, 0, "尾2")
                    yield ResponseCompleted(_response(text="二轮首尾1尾2"))

            yield _events()

    async def scenario() -> None:
        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, StreamingProvider()),
            tool_runtime=cast(
                RunToolRuntime,
                _ToolRuntime(
                    ToolExecutionResult(ToolOutcome(status=ToolOutcomeStatus.OK, code="ok"))
                ),
            ),
            publisher=cast(RunEventTransport, RecordingPublisher()),
        )
        await executor.execute(lease)
        assert repository.run.status is RunStatus.COMPLETED

    asyncio.run(scenario())
    assert [item[2] for item in published] == [
        "首包",
        "abc",
        ("中" * 50) + ("文" * 40),
        "tail-before-tool",
        "二轮首",
        "尾1尾2",
    ]
    assert [item[3] for item in published] == [1, 1, 1, 1, 2, 2]
    assert "publish:tail-before-tool" in call_log


def test_executor_text_delta_batching_error_and_cancellation_boundaries() -> None:
    """首条 TextDelta 即禁重试、普通异常前 flush，取消路径跳过 flush 且不吞 CancelledError。"""
    import time

    from domain.llm.model import TextDelta
    from domain.run.ports import RunEventTransport

    # 场景 1：收到 TextDelta 后立刻抛可重试异常，严守已吐字禁重试契约且 flush 已缓冲分片。
    async def scenario_no_retry_on_first_delta() -> None:
        attempts = 0
        published: list[str] = []

        class Publisher:
            async def publish_text(
                self, run_id: str, execution_epoch: int, text: str, *, round: int = 0
            ) -> None:
                del run_id, execution_epoch, round
                published.append(text)

            async def publish_persisted(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

        class MidStreamFailingProvider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                nonlocal attempts
                del runtime, request
                attempts += 1

                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    yield TextDelta(0, 0, "first")
                    yield TextDelta(0, 0, "buffered-before-err")
                    raise LlmProviderError("llm_temporary_upstream_error", retry_safe=True)

                yield _events()

        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, MidStreamFailingProvider()),
            publisher=cast(RunEventTransport, Publisher()),
        )
        await executor.execute(lease)
        assert attempts == 1
        assert published == ["first", "buffered-before-err"]
        assert repository.run.status is RunStatus.FAILED

    # 场景 2：CancelledError 路径跳过 flush，防止慢 Redis 阻塞取消收尾或吞掉 CancelledError。
    async def scenario_cancel_skips_flush() -> None:
        published: list[str] = []
        entered_block = asyncio.Event()

        class BlockingPublisher:
            async def publish_text(
                self, run_id: str, execution_epoch: int, text: str, *, round: int = 0
            ) -> None:
                del run_id, execution_epoch, round
                published.append(text)
                if len(published) > 1:
                    raise AssertionError("cancel_path_must_not_flush_pending_batch")

            async def publish_persisted(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

        class BlockingProvider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                del runtime, request

                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    yield TextDelta(0, 0, "first-fast")
                    yield TextDelta(0, 0, "buffered-must-skip-on-cancel")
                    entered_block.set()
                    await asyncio.Event().wait()

                yield _events()

        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, BlockingProvider()),
            publisher=cast(RunEventTransport, BlockingPublisher()),
        )
        state = _ExecutionState(
            lease=lease,
            run=lease.run,
            loop_deadline=time.monotonic() + 30.0,
            authorized=None,
            snapshot=_snapshot(),
            main_prompt="prompt",
            compression_prompt="compress",
            budget=RunLlmBudget(
                cast(RunRepository, repository),
                ToolExecutionContext(
                    owner_id=lease.run.owner_id,
                    chat_session_id=lease.run.session_id,
                    project_id=None,
                    run_id=lease.run.id,
                    execution_epoch=lease.run.execution_epoch,
                    auth_session_id=lease.run.auth_session_id or "",
                    deadline_at=lease.execution_deadline_at,
                ),
                used=0,
                maximum=4,
                worker_id=lease.run.worker_id or "",
            ),
            retry_context=(),
            input_files=(),
            event_cursor=0,
            round=1,
        )
        task = asyncio.create_task(
            executor._complete_with_retry(
                state,
                LlmRequest("tools-model", ()),
                allow_retry=True,
            )
        )
        await entered_block.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert published == ["first-fast"]

    # 场景 3：仅收到空 TextDelta("") 后抛可重试异常，同样立即置 emitted_output=True 禁止重试。
    async def scenario_empty_delta_forbids_retry() -> None:
        attempts = 0

        class EmptyDeltaFailingProvider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                nonlocal attempts
                del runtime, request
                attempts += 1

                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    yield TextDelta(0, 0, "")
                    raise LlmProviderError("llm_temporary_upstream_error", retry_safe=True)

                yield _events()

        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, EmptyDeltaFailingProvider()),
        )
        await executor.execute(lease)
        assert attempts == 1
        assert repository.run.status is RunStatus.FAILED

    # 场景 4：publish_text 抛错仅记 warning，不中断模型调用且清空缓冲不重发。
    async def scenario_publish_error_is_tolerated() -> None:
        attempts_publish: list[str] = []

        class FailingPublisher:
            async def publish_text(
                self, run_id: str, execution_epoch: int, text: str, *, round: int = 0
            ) -> None:
                del run_id, execution_epoch, round
                attempts_publish.append(text)
                raise RuntimeError("redis down")

            async def publish_persisted(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

        class NormalProvider:
            @asynccontextmanager
            async def stream(
                self, runtime: RuntimeModelProfile, request: LlmRequest
            ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                del runtime, request

                async def _events() -> AsyncIterator[LlmStreamEvent]:
                    yield TextDelta(0, 0, "p1")
                    yield TextDelta(0, 0, "p2")
                    yield TextDelta(0, 0, "p3")
                    yield ResponseCompleted(_response(text="p1p2p3"))

                yield _events()

        lease = _lease()
        repository = _Repository(lease.run)
        executor = RunExecutor(
            cast(RunRepository, repository),
            cast(ModelProfileRepository, _Profiles()),
            cast(LlmStreamProvider, NormalProvider()),
            publisher=cast(RunEventTransport, FailingPublisher()),
        )
        await executor.execute(lease)
        assert repository.run.status is RunStatus.COMPLETED
        assert attempts_publish == ["p1", "p2p3"]

    asyncio.run(scenario_no_retry_on_first_delta())
    asyncio.run(scenario_cancel_skips_flush())
    asyncio.run(scenario_empty_delta_forbids_retry())
    asyncio.run(scenario_publish_error_is_tolerated())
