"""Run tool catalog, built-in dispatch, and delivery permission boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest

from app.knowledge.service import KnowledgeService
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeService
from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    MCP_DELIVERY_AUTH_ERROR,
    REQUEST_USER_INPUT_DEFINITION,
    SAVE_USER_MEMORY_DEFINITION,
    RunToolRuntimeService,
)
from domain.auth.model import User, UserRole, UserStatus
from domain.knowledge.model import ChunkLocator, KnowledgeBase, SearchHit, SearchResult
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpRuntimeToolTarget,
)
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
            role=UserRole.MEMBER,
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
    def __init__(self, definitions: tuple[RuntimeToolDefinition, ...] = ()) -> None:
        self.definitions = definitions
        self.target_requests: list[tuple[str, str, str, int]] = []

    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
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


class _PreferencesStub:
    """最小偏好服务替身，用于测试 save_user_memory 开关暴露。"""

    def __init__(self, *, memory_enabled: bool = True) -> None:
        self.memory_enabled = memory_enabled

    async def is_memory_enabled(self, user_id: str) -> bool:
        del user_id
        return self.memory_enabled


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


def _mcp_snapshot_definition(*, requires_approval: bool = False) -> RuntimeToolDefinition:
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
        source=McpToolSource(
            server_id="mcp_1",
            remote_name="remote_lookup",
            config_version=2,
        ),
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
    preferences: PreferenceService | _PreferencesStub | None = None,
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
            preferences=None if preferences is None else cast(PreferenceService, preferences),
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

        hidden, _ = _runtime(preferences=_PreferencesStub(memory_enabled=False), project_id=None)
        v2_hidden = await hidden.build_snapshot("run-v2", _authorized())
        assert tuple(item.model_name for item in v2_hidden.tools) == ("request_user_input",)

        knowledge = _Knowledge(SearchResult((), ()))
        projects = _ProjectKnowledge([_base(1)])
        catalog = _McpCatalog((_mcp_snapshot_definition(requires_approval=True),))
        visible, _ = _runtime(
            preferences=_PreferencesStub(memory_enabled=True),
            knowledge=knowledge,
            project_knowledge=projects,
            mcp_catalog=catalog,
        )
        v2 = await visible.build_snapshot("run-v2", _authorized())
        assert tuple(item.model_name for item in v2.tools) == (
            "request_user_input",
            "save_user_memory",
            "knowledge_search",
            "lookup",
        )
        versions = {
            item.model_name: item.source.version
            for item in v2.tools
            if item.source.kind == "builtin"
        }
        assert versions["save_user_memory"] == 1
        assert versions["knowledge_search"] == 1
        save_tool = next(item for item in v2.tools if item.model_name == "save_user_memory")
        assert save_tool.requires_approval is False
        assert v2.tools[-1].requires_approval is True
        assert projects.list_calls == [("owner_1", "project_1")]

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
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
            {"query": " ", "limit": True},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )
        oversized = await runtime.execute(
            SAVE_USER_MEMORY_DEFINITION,
            "save_user_memory",
            {"content": "x" * (65 * 1024)},
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


def test_knowledge_query_uses_server_owned_scope() -> None:
    async def scenario() -> None:
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
            knowledge=knowledge,
            project_knowledge=projects,
        )

        knowledge_outcome = await runtime.execute(
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
            {"query": "design"},
            _context(),
            deadline_seconds=2,
            run_remaining_seconds=10,
        )

        assert knowledge.calls == [("owner_1", ["kb_1"], "design", 5, 2000)]
        assert knowledge_outcome.outcome.code == "knowledge_degraded"
        assert knowledge_outcome.outcome.data["items"][0]["base_id"] == "kb_1"

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


def test_mcp_and_builtin_drop_content_when_delivery_auth_revoked() -> None:
    """MCP 与内置工具在交付前权限撤销时均必须丢弃原始载荷并终止 Run。"""

    async def scenario() -> None:
        auth = _Auth()
        auth.allow_next_calls(1)
        executor = _McpExecutor(
            McpCallResult(
                McpCallStatus.OK,
                "mcp_tool_completed",
                text="sensitive mcp payload",
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
        assert mcp.outcome.status is ToolOutcomeStatus.ERROR
        assert mcp.outcome.code == "tool_permission_revoked"
        assert mcp.outcome.data == {}
        assert "sensitive mcp payload" not in mcp.outcome.canonical_output()
        assert mcp.terminate_run_error_code == MCP_DELIVERY_AUTH_ERROR
        assert catalog.target_requests == [("tool_lookup", "mcp_1", "remote_lookup", 2)]

        knowledge = _Knowledge(
            SearchResult(
                (
                    SearchHit(
                        chunk_id="chunk_secret",
                        document_id="doc_1",
                        version_id="version_1",
                        title="Private",
                        text="private body",
                        locators=(),
                        channels=("keyword",),
                        rank=1.0,
                        knowledge_base_id="kb_1",
                    ),
                ),
                ("keyword",),
            )
        )
        auth = _Auth()
        auth.allow_next_calls(1)
        builtin_runtime, _ = _runtime(
            auth=auth,
            knowledge=knowledge,
            project_knowledge=_ProjectKnowledge([_base(1)]),
        )
        builtin = await builtin_runtime.execute(
            KNOWLEDGE_SEARCH_DEFINITION,
            "knowledge_search",
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


def test_mcp_delivery_auth_timeout_exhausted_and_error_discard_payload() -> None:
    """MCP 交付前鉴权超时、剩余时间耗尽或抛异常时，均不得回传原始载荷。"""

    class _SlowOrFailingAuth(_Auth):
        def __init__(self, *, mode: Literal["timeout", "error"]) -> None:
            super().__init__()
            self._mode = mode

        async def get_current_session_user(
            self, user_id: str, session_id: str, now: datetime
        ) -> User | None:
            if self.requests >= 1:
                self.requests += 1
                if self._mode == "timeout":
                    await asyncio.sleep(5)
                raise RuntimeError("auth_backend_down")
            return await super().get_current_session_user(user_id, session_id, now)

    async def scenario() -> None:
        executor = _McpExecutor(
            McpCallResult(
                McpCallStatus.OK,
                "mcp_tool_completed",
                text="TOP_SECRET_MCP_DATA",
                structured={"secret": "TOP_SECRET_MCP_DATA"},
            )
        )
        catalog = _McpCatalog((_mcp_snapshot_definition(),))

        # 1. 交付前鉴权超时
        timeout_runtime, _ = _runtime(
            auth=_SlowOrFailingAuth(mode="timeout"),
            mcp_catalog=catalog,
            mcp_executor=executor,
        )
        timeout_res = await timeout_runtime.execute(
            _mcp_definition(),
            "lookup",
            {"query": "value"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=0.05,
        )
        # 2. 交付前 Run 剩余时间已耗尽
        exhausted_runtime, _ = _runtime(
            auth=_Auth(),
            mcp_catalog=catalog,
            mcp_executor=executor,
        )
        exhausted_res = await exhausted_runtime.execute(
            _mcp_definition(),
            "lookup",
            {"query": "value"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=0.0,
        )
        # 3. 交付前鉴权抛出异常
        error_runtime, _ = _runtime(
            auth=_SlowOrFailingAuth(mode="error"),
            mcp_catalog=catalog,
            mcp_executor=executor,
        )
        error_res = await error_runtime.execute(
            _mcp_definition(),
            "lookup",
            {"query": "value"},
            _context(),
            deadline_seconds=1,
            run_remaining_seconds=10,
        )

        for result in (timeout_res, exhausted_res, error_res):
            assert result.outcome.status is ToolOutcomeStatus.ERROR
            assert result.outcome.code == "tool_permission_revoked"
            assert result.outcome.data == {}
            assert "TOP_SECRET_MCP_DATA" not in result.outcome.canonical_output()
            assert result.terminate_run_error_code == MCP_DELIVERY_AUTH_ERROR

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


def test_bounded_outcome_terminates_for_minimum_limit_with_long_chinese_message() -> None:
    """512 字节最小上限搭配超长中文 message 与引号转义时必须立即终止且满足字节上限。"""
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.ERROR,
        code="tool_error_" + "x" * 64,
        message=('中文错误详情"带引号"与\\反斜杠' * 80)[:2000],
        data={"payload": "长内容" * 500},
    )
    bounded = bounded_outcome(outcome, maximum_bytes=512)
    encoded = bounded.canonical_output().encode("utf-8")
    assert len(encoded) <= 512
    assert bounded.truncated is True
    assert bounded.data == {}
    parsed = json.loads(encoded.decode("utf-8"))
    assert parsed["code"] == outcome.code
    assert parsed["truncated"] is True


def test_snapshot_rejects_duplicate_model_names_and_oversized_catalogs() -> None:
    duplicate = RuntimeToolDefinition(
        tool_id="builtin_knowledge_search",
        model_name="knowledge_search",
        description="duplicate",
        input_schema={"type": "object"},
        source=BuiltinToolSource(version=1),
    )
    try:
        RuntimeToolSnapshot(tools=(KNOWLEDGE_SEARCH_DEFINITION, duplicate))
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
