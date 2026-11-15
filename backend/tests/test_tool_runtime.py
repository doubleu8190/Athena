"""Run tool catalog, built-in dispatch, and delivery permission boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest

from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.project.knowledge import ProjectKnowledgeService
from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    MEMORY_CORRECT_DEFINITION,
    MEMORY_FORGET_DEFINITION,
    MEMORY_REMEMBER_DEFINITION,
    MEMORY_SEARCH_DEFINITION,
    REQUEST_USER_INPUT_DEFINITION,
    RunToolRuntimeService,
    _current_builtin_version,
    _memory_control_error_code,
)
from domain.auth.model import User, UserRole, UserStatus
from domain.knowledge.model import ChunkLocator, KnowledgeBase, SearchHit, SearchResult
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpRuntimeToolDefinition,
    McpRuntimeToolTarget,
)
from domain.memory.lifecycle import ControlReceiptResult, LoadPolicy, MemoryKind
from domain.memory.model import AccessContext, Memory, MemoryHit, RecallResult
from domain.session.model import Session
from domain.tool import (
    AuthorizedRunContext,
    BuiltinToolSource,
    McpToolSource,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolExecutionContext,
    ToolOutcome,
    ToolOutcomeStatus,
    bounded_outcome,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)


class _Auth:
    def __init__(self, *, authorize_calls: int = 10**6) -> None:
        self.authorize_calls = authorize_calls
        self.requests = 0

    async def get_current_session_user(
        self, user_id: str, session_id: str, now: datetime
    ) -> User | None:
        del session_id, now
        self.requests += 1
        if self.requests > self.authorize_calls:
            return None
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

    def allow_next_calls(self, count: int) -> None:
        self.authorize_calls = self.requests + count


class _Sessions:
    def __init__(self, project_id: str | None) -> None:
        self.project_id = project_id

    async def get_visible(self, scope: Any) -> Session | None:
        if scope.session_id != "session_1" or scope.owner_id != "owner_1":
            return None
        return Session(
            id=scope.session_id,
            owner_id=scope.owner_id,
            title=None,
            project_id=self.project_id,
            last_message_sequence=0,
            created_at=NOW,
            updated_at=NOW,
            deleted_time=None,
        )


class _McpCatalog:
    def __init__(self, definitions: tuple[McpRuntimeToolDefinition, ...] = ()) -> None:
        self.definitions = definitions
        self.target_requests: list[tuple[str, str, str, int]] = []

    async def list_runtime_tools(self, *, limit: int) -> tuple[McpRuntimeToolDefinition, ...]:
        assert limit == 201
        return self.definitions[:201]

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        self.target_requests.append((tool_id, server_id, remote_name, expected_config_version))
        return cast(McpRuntimeToolTarget, object())


class _McpExecutor:
    def __init__(self, result: McpCallResult) -> None:
        self.result = result
        self.calls: list[tuple[object, dict[str, object], float]] = []

    async def call(
        self,
        target: object,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        self.calls.append((target, dict(arguments), deadline_seconds))
        return self.result


class _Memory:
    def __init__(self, result: RecallResult) -> None:
        self.result = result
        self.requests: list[Any] = []
        self.control_calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.control_attempts: list[str] = []
        self.control_error: Exception | None = None
        self.recovery_result: ControlReceiptResult | None = None
        self.recovery_error: Exception | None = None
        self.recovery_calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.rejected_target_ids: set[str] = set()

    async def recall(self, request: Any) -> RecallResult:
        self.requests.append(request)
        return self.result

    async def remember(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None = None,
    ) -> ControlReceiptResult:
        """记录服务端 scope 与控制参数，并返回已提交回执。"""
        self.control_attempts.append("remember")
        arguments = {
            "run_id": run_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "content": content,
            "scope": scope,
            "kind": kind,
            "load_policy": load_policy,
            "evidence": evidence,
            "expires_at": expires_at,
        }
        self.control_calls.append(("remember", access, arguments))
        if self.control_error is not None:
            raise self.control_error
        return ControlReceiptResult(
            operation="remember",
            generation=4,
            memory_id="memory_new",
        )

    async def correct(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        target_memory_id: str,
        expected_version: int,
        expires_at: datetime | None = None,
    ) -> ControlReceiptResult:
        """记录纠正参数，并允许测试模拟响应丢失。"""
        self.control_attempts.append("correct")
        arguments = {
            "run_id": run_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "content": content,
            "scope": scope,
            "kind": kind,
            "load_policy": load_policy,
            "evidence": evidence,
            "target_memory_id": target_memory_id,
            "expected_version": expected_version,
            "expires_at": expires_at,
        }
        self.control_calls.append(("correct", access, arguments))
        if target_memory_id in self.rejected_target_ids:
            raise ValueError("memory_control_target_invalid")
        if self.control_error is not None:
            raise self.control_error
        return ControlReceiptResult(
            operation="correct",
            generation=4,
            memory_id="memory_new",
            superseded_memory_id="memory_old",
        )

    async def forget(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        scope: str,
        evidence: str,
        target_memory_id: str | None,
        expected_version: int | None,
        clear_scope: bool,
    ) -> ControlReceiptResult:
        """记录忘记参数，并允许测试模拟响应丢失。"""
        self.control_attempts.append("forget")
        arguments = {
            "run_id": run_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "scope": scope,
            "evidence": evidence,
            "target_memory_id": target_memory_id,
            "expected_version": expected_version,
            "clear_scope": clear_scope,
        }
        self.control_calls.append(("forget", access, arguments))
        if self.control_error is not None:
            raise self.control_error
        return ControlReceiptResult(operation="forget", generation=4)

    async def recover_control(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        operation: Literal["remember", "correct"],
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None,
        target_memory_id: str | None,
        expected_version: int | None,
    ) -> ControlReceiptResult | None:
        """记录正文控制回执查询，不再次执行控制。"""
        arguments = {
            "run_id": run_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "operation": operation,
            "content": content,
            "scope": scope,
            "kind": kind,
            "load_policy": load_policy,
            "evidence": evidence,
            "expires_at": expires_at,
            "target_memory_id": target_memory_id,
            "expected_version": expected_version,
        }
        self.recovery_calls.append(("control", access, arguments))
        if self.recovery_error is not None:
            raise self.recovery_error
        return self.recovery_result

    async def recover_forget_control(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        scope: str,
        evidence: str,
        target_memory_id: str | None,
        expected_version: int | None,
        clear_scope: bool,
    ) -> ControlReceiptResult | None:
        """记录只读忘记回执查询，不再次执行控制。"""
        arguments = {
            "run_id": run_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "scope": scope,
            "evidence": evidence,
            "target_memory_id": target_memory_id,
            "expected_version": expected_version,
            "clear_scope": clear_scope,
        }
        self.recovery_calls.append(("forget", access, arguments))
        if self.recovery_error is not None:
            raise self.recovery_error
        return self.recovery_result


class _Knowledge:
    def __init__(self, result: SearchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str], str, int, int]] = []

    async def search(
        self,
        owner_id: str,
        base_ids: list[str],
        query: str,
        *,
        limit: int,
        context_budget: int,
    ) -> SearchResult:
        self.calls.append((owner_id, list(base_ids), query, limit, context_budget))
        return self.result


class _ProjectKnowledge:
    def __init__(self, bases: list[KnowledgeBase]) -> None:
        self.bases = bases
        self.list_calls: list[tuple[str, str]] = []

    async def list(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
        self.list_calls.append((owner_id, project_id))
        return list(self.bases)


def _base(index: int) -> KnowledgeBase:
    return KnowledgeBase(
        id=f"kb_{index}",
        owner_id="owner_1",
        name=f"Base {index}",
        description=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _mcp_definition() -> RuntimeToolDefinition:
    return RuntimeToolDefinition(
        tool_id="tool_lookup",
        model_name="lookup",
        description="Look up a value.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source=McpToolSource(server_id="mcp_1", remote_name="remote_lookup", config_version=2),
    )


def _mcp_snapshot_definition(*, requires_approval: bool = False) -> McpRuntimeToolDefinition:
    return McpRuntimeToolDefinition(
        tool_id="tool_lookup",
        server_id="mcp_1",
        remote_name="remote_lookup",
        model_name="lookup",
        description="Look up a value.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        config_version=2,
        requires_approval=requires_approval,
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        owner_id="owner_1",
        chat_session_id="session_1",
        project_id="project_1",
        run_id="run_1",
        execution_epoch=1,
        auth_session_id="auth_1",
        deadline_at=NOW,
    )


def _runtime(
    *,
    auth: _Auth | None = None,
    project_id: str | None = "project_1",
    memory: _Memory | None = None,
    memory_enabled: bool = False,
    knowledge: _Knowledge | None = None,
    project_knowledge: _ProjectKnowledge | None = None,
    mcp_catalog: _McpCatalog | None = None,
    mcp_executor: _McpExecutor | None = None,
) -> tuple[RunToolRuntimeService, _Auth]:
    selected_auth = auth or _Auth()
    return (
        RunToolRuntimeService(
            auth=selected_auth,
            sessions=cast(Any, _Sessions(project_id)),
            mcp_catalog=mcp_catalog or _McpCatalog(),
            mcp_executor=mcp_executor or _McpExecutor(McpCallResult(McpCallStatus.OK, "ok")),
            memory=None if memory is None else cast(MemoryService, memory),
            memory_enabled=memory_enabled,
            knowledge=None if knowledge is None else cast(KnowledgeService, knowledge),
            project_knowledge=(
                None
                if project_knowledge is None
                else cast(ProjectKnowledgeService, project_knowledge)
            ),
        ),
        selected_auth,
    )


def _authorized() -> AuthorizedRunContext:
    return AuthorizedRunContext(
        owner_id="owner_1",
        chat_session_id="session_1",
        project_id="project_1",
        auth_session_id="auth_1",
    )


def test_snapshot_rejects_unsupported_version_and_hides_unavailable_v2_builtins() -> None:
    async def scenario() -> None:
        runtime, _ = _runtime(project_id=None)
        # 历史 v1 版本不再兼容，必须在构建快照时直接拒绝。
        with pytest.raises(ValueError, match="run_prompt_template_unsupported"):
            await runtime.build_snapshot("run-v1", _authorized())

        hidden, _ = _runtime(memory_enabled=False, project_id=None)
        v2_hidden = await hidden.build_snapshot("run-v2", _authorized())
        assert tuple(item.model_name for item in v2_hidden.tools) == ("request_user_input",)

        memory = _Memory(RecallResult("ok"))
        knowledge = _Knowledge(SearchResult((), ()))
        projects = _ProjectKnowledge([_base(1)])
        catalog = _McpCatalog((_mcp_snapshot_definition(requires_approval=True),))
        visible, _ = _runtime(
            memory=memory,
            memory_enabled=True,
            knowledge=knowledge,
            project_knowledge=projects,
            mcp_catalog=catalog,
        )
        v2 = await visible.build_snapshot("run-v2", _authorized())
        assert tuple(item.model_name for item in v2.tools) == (
            "request_user_input",
            "memory_search",
            "memory_remember",
            "memory_correct",
            "memory_forget",
            "knowledge_search",
            "lookup",
        )
        versions = {
            item.model_name: item.source.version
            for item in v2.tools
            if item.source.kind == "builtin"
        }
        assert versions["memory_remember"] == 2
        assert versions["memory_correct"] == 2
        assert versions["memory_search"] == 1
        assert versions["memory_forget"] == 1
        assert versions["knowledge_search"] == 1
        assert v2.tools[-1].requires_approval is True
        assert projects.list_calls == [("owner_1", "project_1")]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error_code",
    [
        "memory_scope_disabled",
        "memory_control_version_conflict",
        "memory_control_version_required",
        "memory_control_run_invalid",
        "memory_control_project_invalid",
        "memory_control_session_invalid",
        "memory_control_source_invalid",
        "memory_control_source_changed",
        "memory_control_generation_changed",
        "memory_control_idempotency_conflict",
        "memory_control_receipt_unavailable",
        "memory_control_receipt_invalid",
    ],
)
def test_memory_control_projects_stable_business_error_codes(error_code: str) -> None:
    """控制工具应保留已定义的稳定业务错误，未知异常才统一脱敏。"""
    assert _memory_control_error_code(error_code) == error_code
    assert _memory_control_error_code("private provider detail") == "memory_control_invalid"


@pytest.mark.parametrize(
    "removed_error_code",
    [
        "memory_control_fact_group_changed",
        "memory_control_fact_group_invalid",
        "memory_control_create_conflict",
        "memory_control_create_invalid",
        "memory_target_ambiguous",
        "memory_target_changed",
        "memory_applicability_invalid",
        "fact_key_invalid",
    ],
)
def test_memory_control_hides_removed_fact_semantic_errors(removed_error_code: str) -> None:
    """事实组与适用条件错误已不属于公开控制协议。"""
    assert _memory_control_error_code(removed_error_code) == "memory_control_invalid"


def test_memory_write_schemas_use_content_without_fact_identity_fields() -> None:
    """工具只提交正文语义，身份、来源、版本和事实判断由服务端负责。"""
    write_properties = {
        "content",
        "scope",
        "kind",
        "load_policy",
        "evidence",
        "expires_at",
    }
    old_fact_properties = {
        "fact_key",
        "fact_value",
        "cardinality",
        "polarity",
        "applicability",
        "claim_intent",
        "subject",
    }

    remember_schema = MEMORY_REMEMBER_DEFINITION.input_schema
    correct_schema = MEMORY_CORRECT_DEFINITION.input_schema
    assert set(remember_schema["properties"]) == write_properties
    assert set(correct_schema["properties"]) == (
        write_properties | {"target_memory_id", "expected_version"}
    )
    assert set(remember_schema["required"]) == write_properties - {"expires_at"}
    assert set(correct_schema["required"]) == (write_properties - {"expires_at"}) | {
        "target_memory_id",
        "expected_version",
    }
    assert old_fact_properties.isdisjoint(remember_schema["properties"])
    assert old_fact_properties.isdisjoint(correct_schema["properties"])


def test_memory_write_schema_rejects_model_owned_identity_and_missing_target() -> None:
    """模型不能伪造 owner 或跨 scope 目标，correct 也必须提供检索版本。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        remember_arguments = {
            "content": "我喜欢苹果",
            "scope": "personal",
            "kind": "preference",
            "load_policy": "on_demand",
            "evidence": "记住，我喜欢苹果",
        }
        forged_owner = {**remember_arguments, "owner_id": "owner_2"}
        forged_scope = {**remember_arguments, "scope": "project:project_2"}
        missing_target = {
            **remember_arguments,
            "content": "改为喜欢香蕉",
            "evidence": "纠正一下，改为喜欢香蕉",
        }

        forged_owner_error = runtime.validate_call(
            MEMORY_REMEMBER_DEFINITION,
            "memory_remember",
            forged_owner,
        )
        forged_scope_error = runtime.validate_call(
            MEMORY_REMEMBER_DEFINITION,
            "memory_remember",
            forged_scope,
        )
        missing_target_error = runtime.validate_call(
            MEMORY_CORRECT_DEFINITION,
            "memory_correct",
            missing_target,
        )
        assert forged_owner_error is not None
        assert forged_scope_error is not None
        assert missing_target_error is not None
        assert forged_owner_error.code == "tool_arguments_invalid"
        assert forged_scope_error.code == "tool_arguments_invalid"
        assert missing_target_error.code == "tool_arguments_invalid"
        assert memory.control_attempts == []

    asyncio.run(scenario())


def test_stale_v1_memory_write_snapshots_are_rejected_before_side_effects() -> None:
    """旧 v1 的 remember/correct 冻结定义必须显式失效，其他 v1 工具仍可执行。"""

    async def scenario() -> None:
        memory = _Memory(
            RecallResult(
                "ok",
                hits=(
                    MemoryHit(
                        memory=Memory(
                            id="mem_1",
                            namespace_id="owner_1",
                            scope_key="personal",
                            content="我喜欢苹果",
                            status="active",
                            origin="explicit",
                            version=2,
                            expires_at=None,
                            sources=(),
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        score=0.9,
                        channels=("keyword",),
                    ),
                ),
            )
        )
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        legacy_properties = {
            **MEMORY_REMEMBER_DEFINITION.input_schema["properties"],
            "fact_key": {"type": "string"},
            "fact_value": {"type": "string"},
            "cardinality": {"type": "string"},
            "polarity": {"type": "string"},
            "applicability": {"type": "object"},
        }
        legacy_required = [
            *MEMORY_REMEMBER_DEFINITION.input_schema["required"],
            "fact_key",
            "fact_value",
            "cardinality",
            "polarity",
            "applicability",
        ]
        remember_v1 = MEMORY_REMEMBER_DEFINITION.model_copy(
            update={
                "source": BuiltinToolSource(version=1),
                "input_schema": {
                    "type": "object",
                    "properties": legacy_properties,
                    "required": legacy_required,
                    "additionalProperties": False,
                },
            }
        )
        correct_v1 = MEMORY_CORRECT_DEFINITION.model_copy(
            update={
                "source": BuiltinToolSource(version=1),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        **MEMORY_CORRECT_DEFINITION.input_schema["properties"],
                        "fact_key": {"type": "string"},
                        "fact_value": {"type": "string"},
                        "cardinality": {"type": "string"},
                        "polarity": {"type": "string"},
                        "applicability": {"type": "object"},
                    },
                    "required": [
                        *MEMORY_CORRECT_DEFINITION.input_schema["required"],
                        "fact_key",
                        "fact_value",
                        "cardinality",
                        "polarity",
                        "applicability",
                    ],
                    "additionalProperties": False,
                },
            }
        )
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_stale",
        )

        remember = await runtime.execute(
            remember_v1,
            "memory_remember",
            {
                "content": "我喜欢苹果",
                "scope": "personal",
                "kind": "preference",
                "load_policy": "on_demand",
                "evidence": "记住，我喜欢苹果",
                "fact_key": "liked_fruit",
                "fact_value": "苹果",
                "cardinality": "single",
                "polarity": "affirmed",
                "applicability": {},
            },
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        correct = await runtime.execute(
            correct_v1,
            "memory_correct",
            {
                "content": "改为喜欢香蕉",
                "scope": "personal",
                "kind": "preference",
                "load_policy": "on_demand",
                "evidence": "纠正一下，改为喜欢香蕉",
                "target_memory_id": "mem_1",
                "expected_version": 2,
                "fact_key": "liked_fruit",
                "fact_value": "香蕉",
                "cardinality": "single",
                "polarity": "affirmed",
                "applicability": {},
            },
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        search = await runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "水果"},
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert remember.outcome.status is ToolOutcomeStatus.ERROR
        assert remember.outcome.code == "tool_version_unavailable"
        assert correct.outcome.status is ToolOutcomeStatus.ERROR
        assert correct.outcome.code == "tool_version_unavailable"
        assert memory.control_attempts == []
        assert search.outcome.status is ToolOutcomeStatus.OK
        assert len(memory.requests) == 1

    asyncio.run(scenario())


def test_prepare_call_rejects_stale_memory_versions_before_schema_and_approval() -> None:
    """旧 v1 remember/correct 在 Schema 校验和审批判定前即明确失效。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        legacy_fact_properties = {
            "fact_key": {"type": "string"},
            "fact_value": {"type": "string"},
            "cardinality": {"type": "string"},
            "polarity": {"type": "string"},
            "applicability": {"type": "object"},
        }
        legacy_remember_schema = {
            "type": "object",
            "properties": {
                **MEMORY_REMEMBER_DEFINITION.input_schema["properties"],
                **legacy_fact_properties,
            },
            "required": [
                *MEMORY_REMEMBER_DEFINITION.input_schema["required"],
                *legacy_fact_properties,
            ],
            "additionalProperties": False,
        }
        legacy_correct_schema = {
            "type": "object",
            "properties": {
                **MEMORY_CORRECT_DEFINITION.input_schema["properties"],
                **legacy_fact_properties,
            },
            "required": [
                *MEMORY_CORRECT_DEFINITION.input_schema["required"],
                *legacy_fact_properties,
            ],
            "additionalProperties": False,
        }
        remember_v1 = MEMORY_REMEMBER_DEFINITION.model_copy(
            update={
                "source": BuiltinToolSource(version=1),
                "requires_approval": True,
                "input_schema": legacy_remember_schema,
            }
        )
        correct_v1 = MEMORY_CORRECT_DEFINITION.model_copy(
            update={
                "source": BuiltinToolSource(version=1),
                "requires_approval": True,
                "input_schema": legacy_correct_schema,
            }
        )
        old_remember_arguments = {
            "content": "我喜欢苹果",
            "scope": "personal",
            "kind": "preference",
            "load_policy": "on_demand",
            "evidence": "记住，我喜欢苹果",
            "fact_key": "liked_fruit",
            "fact_value": "苹果",
            "cardinality": "single",
            "polarity": "affirmed",
            "applicability": {},
        }
        old_correct_arguments = {
            "content": "改为喜欢香蕉",
            "scope": "personal",
            "kind": "preference",
            "load_policy": "on_demand",
            "evidence": "纠正一下，改为喜欢香蕉",
            "target_memory_id": "mem_1",
            "expected_version": 2,
            "fact_key": "liked_fruit",
            "fact_value": "香蕉",
            "cardinality": "single",
            "polarity": "affirmed",
            "applicability": {},
        }
        new_remember_arguments = {
            key: value
            for key, value in old_remember_arguments.items()
            if key in MEMORY_REMEMBER_DEFINITION.input_schema["properties"]
        }
        new_correct_arguments = {
            key: value
            for key, value in old_correct_arguments.items()
            if key in MEMORY_CORRECT_DEFINITION.input_schema["properties"]
        }
        cases: tuple[tuple[RuntimeToolDefinition, dict[str, Any]], ...] = (
            (remember_v1, old_remember_arguments),
            (correct_v1, old_correct_arguments),
            (remember_v1, new_remember_arguments),
            (correct_v1, new_correct_arguments),
            (remember_v1, {"content": 7}),
            (correct_v1, {"target_memory_id": 7}),
        )

        for index, (definition, arguments) in enumerate(cases):
            preparation = await runtime.prepare_call(
                definition,
                definition.model_name,
                arguments,
                cast(Any, object()),
                _authorized(),
                call_id=f"stale_{index}",
            )
            assert preparation.outcome is not None
            assert preparation.outcome.code == "tool_version_unavailable"
            assert preparation.requires_approval is False

        assert memory.control_attempts == []

    asyncio.run(scenario())


def test_builtin_versions_upgrade_only_memory_write_tools() -> None:
    """版本映射只提升 remember/correct，不使其他 v1 内置工具整体失效。"""

    async def scenario() -> None:
        runtime, _ = _runtime(memory=_Memory(RecallResult("ok")), memory_enabled=True)
        assert _current_builtin_version(MEMORY_REMEMBER_DEFINITION) == 2
        assert _current_builtin_version(MEMORY_CORRECT_DEFINITION) == 2
        assert _current_builtin_version(MEMORY_SEARCH_DEFINITION) == 1
        assert _current_builtin_version(MEMORY_FORGET_DEFINITION) == 1
        assert _current_builtin_version(REQUEST_USER_INPUT_DEFINITION) == 1

        search = await runtime.prepare_call(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "水果"},
            cast(Any, object()),
            _authorized(),
            call_id="search_v1",
        )
        forget = await runtime.prepare_call(
            MEMORY_FORGET_DEFINITION,
            "memory_forget",
            {
                "scope": "personal",
                "evidence": "忘记苹果偏好",
                "target_memory_id": "mem_1",
                "expected_version": 2,
                "clear_scope": False,
            },
            cast(Any, object()),
            _authorized(),
            call_id="forget_v1",
        )
        assert search.outcome is None
        assert search.requires_approval is False
        assert forget.outcome is None
        assert forget.requires_approval is True

    asyncio.run(scenario())


def test_validate_call_rejects_invalid_schema_before_any_provider_call() -> None:
    async def scenario() -> None:
        executor = _McpExecutor(McpCallResult(McpCallStatus.OK, "ok"))
        runtime, _ = _runtime(mcp_executor=executor)
        definition = _mcp_definition()

        assert runtime.validate_call(definition, "lookup", {"query": "value"}) is None
        invalid = runtime.validate_call(definition, "lookup", {"query": 1})
        oversized = runtime.validate_call(
            definition,
            "lookup",
            {"query": "x" * (65 * 1024)},
        )
        unknown = runtime.validate_call(None, "missing", {})
        assert invalid is not None and invalid.code == "tool_arguments_invalid"
        assert oversized is not None and oversized.code == "tool_arguments_too_large"
        assert unknown is not None and unknown.code == "tool_unknown"
        assert executor.calls == []

    asyncio.run(scenario())


def test_unknown_invalid_and_oversized_arguments_never_dispatch() -> None:
    async def scenario() -> None:
        executor = _McpExecutor(McpCallResult(McpCallStatus.OK, "ok"))
        runtime, _ = _runtime(mcp_executor=executor)

        unknown = await runtime.execute(
            None,
            "guessed",
            {},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        invalid = await runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": " ", "limit": True},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        oversized = await runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "x" * (65 * 1024)},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        definition = _mcp_definition()
        schema_mismatch = await runtime.execute(
            definition,
            "lookup",
            {"query": 1},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        assert unknown.outcome.code == "tool_unknown"
        assert invalid.outcome.code == "tool_arguments_invalid"
        assert oversized.outcome.code == "tool_arguments_too_large"
        assert schema_mismatch.outcome.code == "tool_arguments_invalid"
        assert executor.calls == []

    asyncio.run(scenario())


def test_memory_and_knowledge_queries_use_server_owned_scope() -> None:
    async def scenario() -> None:
        memory_hit = MemoryHit(
            memory=Memory(
                id="mem_1",
                namespace_id="owner_1",
                scope_key="project:project_1",
                content="Use the approved database.",
                status="active",
                origin="explicit",
                version=1,
                expires_at=None,
                sources=(),
                created_at=NOW,
                updated_at=NOW,
            ),
            score=0.9,
            channels=("keyword",),
        )
        memory = _Memory(
            RecallResult(
                "degraded",
                hits=(memory_hit,),
                truncated=True,
                degradation_reasons=("semantic_unavailable",),
            )
        )
        knowledge_hit = SearchHit(
            chunk_id="chunk_1",
            document_id="doc_1",
            version_id="version_1",
            title="Design",
            text="Approved design",
            locators=(ChunkLocator(kind="page", page=2),),
            channels=("keyword",),
            rank=0.8,
            knowledge_base_id="kb_1",
        )
        knowledge = _Knowledge(
            SearchResult(
                (knowledge_hit,),
                ("keyword",),
                degraded_reasons=("semantic_unavailable",),
            )
        )
        projects = _ProjectKnowledge([_base(1)])
        runtime, _ = _runtime(
            memory=memory,
            memory_enabled=True,
            knowledge=knowledge,
            project_knowledge=projects,
        )

        memory_outcome = await runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "database", "limit": 3},
            _context(),
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        knowledge_outcome = await runtime.execute(
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
            {"query": "design"},
            _context(),
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        recall_request = memory.requests[0]
        assert recall_request.access.namespace_id == "owner_1"
        assert recall_request.access.readable_scopes == (
            "personal",
            "project:project_1",
        )
        assert recall_request.scope_priority == ("project:project_1", "personal")
        assert recall_request.limit == 3
        assert recall_request.token_budget == 1200
        assert memory_outcome.outcome.code == "memory_degraded"
        assert memory_outcome.outcome.data["items"][0] == {
            "memory_id": "mem_1",
            "version": 1,
            "content": "Use the approved database.",
            "scope": "project",
        }
        assert knowledge.calls == [("owner_1", ["kb_1"], "design", 5, 2000)]
        assert knowledge_outcome.outcome.code == "knowledge_degraded"
        assert knowledge_outcome.outcome.data["items"][0]["base_id"] == "kb_1"

    asyncio.run(scenario())


def test_memory_remember_uses_server_scope_source_and_persisted_call_identity() -> None:
    """控制工具必须把 Run、Session、调用 ID 和直接证据交给同事务服务。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_1",
        )
        arguments = {
            "content": "本项目禁止修改原仓库",
            "scope": "project",
            "kind": "constraint",
            "load_policy": "always",
            "evidence": "本项目禁止修改原仓库",
        }

        result = await runtime.execute(
            MEMORY_REMEMBER_DEFINITION,
            "memory_remember",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert result.outcome.status is ToolOutcomeStatus.OK
        assert result.outcome.code == "memory_control_ok"
        assert result.outcome.data["memory_id"] == "memory_new"
        assert result.outcome.data["generation"] == 4
        operation, access, received = memory.control_calls[0]
        assert operation == "remember"
        assert access.namespace_id == "owner_1"
        assert access.readable_scopes == ("personal", "project:project_1")
        assert access.writable_scopes == ("personal", "project:project_1")
        assert received["run_id"] == "run_1"
        assert received["session_id"] == "session_1"
        assert received["tool_call_id"] == "record_1"
        assert received["evidence"] == "本项目禁止修改原仓库"
        assert set(received) == {
            "run_id",
            "session_id",
            "tool_call_id",
            "content",
            "scope",
            "kind",
            "load_policy",
            "evidence",
            "expires_at",
        }

        memory.control_error = ValueError("memory_control_idempotency_conflict")
        conflict = await runtime.execute(
            MEMORY_REMEMBER_DEFINITION,
            "memory_remember",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        assert conflict.outcome.code == "memory_control_idempotency_conflict"
        assert conflict.outcome.data == {}

    asyncio.run(scenario())


def test_memory_correct_uses_search_target_and_rejects_foreign_target() -> None:
    """correct 的目标必须来自当前用户检索结果，伪造目标由 service 拒绝。"""

    async def scenario() -> None:
        memory_hit = MemoryHit(
            memory=Memory(
                id="mem_1",
                namespace_id="owner_1",
                scope_key="project:project_1",
                content="数据库使用 PostgreSQL",
                status="active",
                origin="explicit",
                version=3,
                expires_at=None,
                sources=(),
                created_at=NOW,
                updated_at=NOW,
            ),
            score=0.9,
            channels=("keyword",),
        )
        memory = _Memory(RecallResult("ok", hits=(memory_hit,)))
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_correct",
        )

        searched = await runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "数据库"},
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        target = searched.outcome.data["items"][0]
        arguments = {
            "content": "数据库使用 PostgreSQL 17",
            "scope": "project",
            "kind": "decision",
            "load_policy": "on_demand",
            "evidence": "纠正一下，数据库使用 PostgreSQL 17",
            "target_memory_id": target["memory_id"],
            "expected_version": target["version"],
        }

        corrected = await runtime.execute(
            MEMORY_CORRECT_DEFINITION,
            "memory_correct",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert corrected.outcome.status is ToolOutcomeStatus.OK
        operation, access, received = memory.control_calls[0]
        assert operation == "correct"
        assert access.namespace_id == "owner_1"
        assert received["target_memory_id"] == "mem_1"
        assert received["expected_version"] == 3

        memory.rejected_target_ids.add("mem_other_owner")
        forged = await runtime.execute(
            MEMORY_CORRECT_DEFINITION,
            "memory_correct",
            {**arguments, "target_memory_id": "mem_other_owner"},
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )
        assert forged.outcome.status is ToolOutcomeStatus.ERROR
        assert forged.outcome.code == "memory_control_target_invalid"
        assert forged.outcome.data == {}
        assert memory.control_calls[-1][1].namespace_id == "owner_1"
        assert memory.control_calls[-1][2]["target_memory_id"] == "mem_other_owner"

    asyncio.run(scenario())


def test_memory_control_unknown_exception_replays_only_the_original_receipt() -> None:
    """提交响应丢失后应查询原参数摘要回执，不能重发业务操作。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        memory.control_error = RuntimeError("commit response lost")
        memory.recovery_result = ControlReceiptResult(
            operation="remember",
            generation=7,
            memory_id="memory_recovered",
        )
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_1",
        )
        arguments = {
            "content": "我喜欢苹果",
            "scope": "personal",
            "kind": "preference",
            "load_policy": "on_demand",
            "evidence": "记住，我喜欢苹果",
        }

        result = await runtime.execute(
            MEMORY_REMEMBER_DEFINITION,
            "memory_remember",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert result.outcome.status is ToolOutcomeStatus.OK
        assert result.outcome.data["memory_id"] == "memory_recovered"
        assert result.outcome.data["generation"] == 7
        assert memory.control_attempts == ["remember"]
        assert len(memory.recovery_calls) == 1
        operation, access, recovered = memory.recovery_calls[0]
        assert operation == "control"
        assert access.namespace_id == "owner_1"
        assert recovered["operation"] == "remember"
        assert recovered["run_id"] == "run_1"
        assert recovered["session_id"] == "session_1"
        assert recovered["tool_call_id"] == "record_1"
        assert recovered["evidence"] == "记住，我喜欢苹果"
        assert recovered["target_memory_id"] is None
        assert recovered["expected_version"] is None

    asyncio.run(scenario())


def test_memory_correct_unknown_recovery_preserves_original_target_and_version() -> None:
    """correct 响应丢失后只按原目标和版本对应的参数摘要查询回执。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        memory.control_error = RuntimeError("commit response lost")
        memory.recovery_result = ControlReceiptResult(
            operation="correct",
            generation=8,
            memory_id="memory_corrected",
            superseded_memory_id="mem_1",
        )
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_correct",
        )
        arguments = {
            "content": "改为喜欢香蕉",
            "scope": "personal",
            "kind": "preference",
            "load_policy": "on_demand",
            "evidence": "纠正一下，改为喜欢香蕉",
            "target_memory_id": "mem_1",
            "expected_version": 3,
        }

        result = await runtime.execute(
            MEMORY_CORRECT_DEFINITION,
            "memory_correct",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert result.outcome.status is ToolOutcomeStatus.OK
        assert result.outcome.data["memory_id"] == "memory_corrected"
        assert memory.control_attempts == ["correct"]
        operation, access, recovered = memory.recovery_calls[0]
        assert operation == "control"
        assert access.namespace_id == "owner_1"
        assert recovered["operation"] == "correct"
        assert recovered["run_id"] == "run_1"
        assert recovered["session_id"] == "session_1"
        assert recovered["tool_call_id"] == "record_correct"
        assert recovered["target_memory_id"] == "mem_1"
        assert recovered["expected_version"] == 3

    asyncio.run(scenario())


def test_memory_control_unknown_without_receipt_is_unknown_and_not_retried() -> None:
    """回执不存在时只能返回 UNKNOWN，当前调用不得再执行一次删除。"""

    async def scenario() -> None:
        memory = _Memory(RecallResult("ok"))
        memory.control_error = RuntimeError("database response lost")
        runtime, _ = _runtime(memory=memory, memory_enabled=True)
        context = ToolExecutionContext(
            owner_id="owner_1",
            chat_session_id="session_1",
            project_id="project_1",
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=NOW,
            tool_call_record_id="record_2",
        )
        arguments = {
            "scope": "personal",
            "evidence": "忘记苹果偏好",
            "target_memory_id": "memory_1",
            "expected_version": 3,
            "clear_scope": False,
        }

        result = await runtime.execute(
            MEMORY_FORGET_DEFINITION,
            "memory_forget",
            arguments,
            context,
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
        assert result.outcome.code == "memory_unavailable"
        assert result.outcome.data == {}
        assert memory.control_attempts == ["forget"]
        assert len(memory.recovery_calls) == 1
        assert memory.recovery_calls[0][0] == "forget"

    asyncio.run(scenario())


def test_knowledge_search_refuses_silent_subset_when_scope_exceeds_limit() -> None:
    async def scenario() -> None:
        knowledge = _Knowledge(SearchResult((), ()))
        projects = _ProjectKnowledge([_base(index) for index in range(21)])
        runtime, _ = _runtime(
            knowledge=knowledge,
            project_knowledge=projects,
        )
        result = await runtime.execute(
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
            {"query": "anything"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        assert result.outcome.status is ToolOutcomeStatus.ERROR
        assert result.outcome.code == "knowledge_scope_unavailable"
        assert knowledge.calls == []

    asyncio.run(scenario())


def test_knowledge_search_drops_body_when_scope_shrinks_during_search() -> None:
    async def scenario() -> None:
        search_started = asyncio.Event()
        release_search = asyncio.Event()

        class _BlockingKnowledge(_Knowledge):
            async def search(
                self,
                owner_id: str,
                base_ids: list[str],
                query: str,
                *,
                limit: int,
                context_budget: int,
            ) -> SearchResult:
                self.calls.append((owner_id, list(base_ids), query, limit, context_budget))
                search_started.set()
                await release_search.wait()
                return self.result

        projects = _ProjectKnowledge([_base(1)])
        knowledge = _BlockingKnowledge(
            SearchResult(
                (
                    SearchHit(
                        chunk_id="chunk_secret",
                        document_id="doc_1",
                        version_id="version_1",
                        title="Formerly visible",
                        text="TOP SECRET BODY",
                        locators=(),
                        channels=("keyword",),
                        rank=1.0,
                        knowledge_base_id="kb_1",
                    ),
                ),
                ("keyword",),
            )
        )
        runtime, _ = _runtime(
            knowledge=knowledge,
            project_knowledge=projects,
        )

        execution = asyncio.create_task(
            runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "secret"},
                _context(),
                deadline_seconds=1,
                run_remaining_seconds=10,
            )
        )
        await search_started.wait()
        projects.bases = []
        release_search.set()
        result = await execution

        assert result.outcome.status is ToolOutcomeStatus.ERROR
        assert result.outcome.code == "knowledge_scope_unavailable"
        assert result.outcome.data == {}
        assert "TOP SECRET BODY" not in result.outcome.canonical_output()
        assert len(projects.list_calls) == 2

    asyncio.run(scenario())


def test_knowledge_search_drops_body_when_scope_recheck_fails() -> None:
    class _PermissionFailingProjects(_ProjectKnowledge):
        async def list(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
            self.list_calls.append((owner_id, project_id))
            if len(self.list_calls) > 1:
                raise PermissionError("scope revoked")
            return list(self.bases)

    async def scenario() -> None:
        projects = _PermissionFailingProjects([_base(1)])
        knowledge = _Knowledge(
            SearchResult(
                (
                    SearchHit(
                        chunk_id="chunk_secret",
                        document_id="doc_1",
                        version_id="version_1",
                        title="Formerly visible",
                        text="TOP SECRET BODY",
                        locators=(),
                        channels=("keyword",),
                        rank=1.0,
                        knowledge_base_id="kb_1",
                    ),
                ),
                ("keyword",),
            )
        )
        runtime, _ = _runtime(
            knowledge=knowledge,
            project_knowledge=projects,
        )

        result = await runtime.execute(
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
            {"query": "secret"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )

        assert result.outcome.status is ToolOutcomeStatus.ERROR
        assert result.outcome.code == "knowledge_scope_unavailable"
        assert result.outcome.data == {}
        assert "TOP SECRET BODY" not in result.outcome.canonical_output()

    asyncio.run(scenario())


def test_mcp_known_success_survives_revocation_but_builtin_content_is_dropped() -> None:
    async def scenario() -> None:
        auth = _Auth()
        auth.allow_next_calls(1)
        executor = _McpExecutor(
            McpCallResult(
                McpCallStatus.OK,
                "mcp_tool_completed",
                text="known external effect",
            )
        )
        catalog = _McpCatalog((_mcp_snapshot_definition(),))
        runtime, _ = _runtime(
            auth=auth,
            mcp_catalog=catalog,
            mcp_executor=executor,
        )
        mcp = await runtime.execute(
            _mcp_definition(),
            "lookup",
            {"query": "value"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        assert mcp.outcome.status is ToolOutcomeStatus.OK
        assert mcp.outcome.data["text"] == "known external effect"
        assert mcp.terminate_run_error_code == "run_auth_session_invalid"
        assert catalog.target_requests == [("tool_lookup", "mcp_1", "remote_lookup", 2)]

        memory = _Memory(
            RecallResult(
                "ok",
                hits=(
                    MemoryHit(
                        memory=Memory(
                            id="mem_secret",
                            namespace_id="owner_1",
                            scope_key="personal",
                            content="private body",
                            status="active",
                            origin="explicit",
                            version=1,
                            expires_at=None,
                            sources=(),
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        score=1.0,
                        channels=("keyword",),
                    ),
                ),
            )
        )
        auth = _Auth()
        auth.allow_next_calls(1)
        builtin_runtime, _ = _runtime(
            auth=auth,
            memory=memory,
            memory_enabled=True,
        )
        builtin = await builtin_runtime.execute(
            MEMORY_SEARCH_DEFINITION,
            "memory_search",
            {"query": "private"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        assert builtin.outcome.status is ToolOutcomeStatus.ERROR
        assert builtin.outcome.code == "tool_permission_revoked"
        assert builtin.outcome.data == {}
        assert "private body" not in builtin.outcome.canonical_output()

    asyncio.run(scenario())


def test_bounded_outcome_preserves_valid_utf8_json_and_truncation_flag() -> None:
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="ok",
        message="message",
        data={"items": ["中文" * 1000, "second"]},
    )
    bounded = bounded_outcome(outcome, maximum_bytes=1024)
    encoded = bounded.canonical_output().encode("utf-8")
    assert len(encoded) <= 1024
    assert bounded.truncated is True
    assert json.loads(encoded)["truncated"] is True


def test_snapshot_rejects_duplicate_model_names_and_oversized_catalogs() -> None:
    duplicate = RuntimeToolDefinition(
        tool_id="builtin_memory_search",
        model_name="memory_search",
        description="duplicate",
        input_schema={"type": "object"},
        source=BuiltinToolSource(version=1),
    )
    try:
        RuntimeToolSnapshot(tools=(MEMORY_SEARCH_DEFINITION, duplicate))
    except ValueError as error:
        assert "tool_snapshot_duplicate" in str(error)
    else:
        raise AssertionError("duplicate snapshot accepted")

    try:
        RuntimeToolDefinition(
            tool_id=REQUEST_USER_INPUT_DEFINITION.tool_id,
            model_name=REQUEST_USER_INPUT_DEFINITION.model_name,
            description="x" * 9000,
            input_schema=dict(REQUEST_USER_INPUT_DEFINITION.input_schema),
            source=REQUEST_USER_INPUT_DEFINITION.source,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("oversized tool accepted")
