"""Subagent 领域校验、工具目录聚合与服务单元测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.subagent import DefaultSubagentToolCatalog, SubagentService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.mcp import (
    McpServer,
    McpServerPage,
    McpServerWrite,
    StreamableHttpConfig,
)
from domain.subagent import (
    DISPATCH_SUBAGENTS_TOOL_ID,
    RESERVED_SUBAGENT_SLUGS,
    CreateSubagentCommand,
    SubagentAdminRequiredError,
    SubagentInvalidSessionError,
    SubagentModelUnavailableError,
    SubagentNotFoundError,
    SubagentPage,
    SubagentProfile,
    SubagentSlugReservedError,
    SubagentToolForbiddenError,
    SubagentUpdatePatch,
    SubagentValidationError,
    UpdateSubagentCommand,
    validate_subagent_allowed_tool_ids,
    validate_subagent_description,
    validate_subagent_max_llm_requests,
    validate_subagent_name,
    validate_subagent_slug,
    validate_subagent_system_prompt,
    validate_subagent_timeout_seconds,
)
from domain.tool import AdminToolDefinition, ToolPage

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


def _actor(role: UserRole = UserRole.ADMIN) -> AuthContext:
    user = User(
        id="usr_admin_1",
        email="admin@example.com",
        password_hash="unused",
        display_name="Admin",
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=role,
    )
    return AuthContext(user=user, session_id="sess_1")


class _StubAuthReader:
    def __init__(self, user: User | None) -> None:
        self._user = user

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del user_id, session_id, now
        return self._user


class _StubModelChecker:
    def __init__(self, available_ids: set[str]) -> None:
        self._available_ids = available_ids

    async def is_model_available(self, model_profile_id: str) -> bool:
        return model_profile_id in self._available_ids


class _StubMcpRepo:
    def __init__(self, tools: tuple[AdminToolDefinition, ...]) -> None:
        self._tools = tools

    async def list_admin(self, *, offset: int, limit: int) -> ToolPage[AdminToolDefinition]:
        sliced = self._tools[offset : offset + limit]
        return ToolPage(items=sliced, total=len(self._tools))

    async def create_with_tools(
        self, actor: AuthContext, server: McpServerWrite, tools: object
    ) -> McpServer:
        raise NotImplementedError

    async def replace_with_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        replacement: McpServerWrite,
        tools: object,
    ) -> McpServer:
        raise NotImplementedError

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        raise NotImplementedError

    async def get(self, server_id: str) -> McpServer | None:
        raise NotImplementedError

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        del offset, limit
        server = McpServer(
            id="mcp_1",
            name="GitHub",
            config=StreamableHttpConfig(
                transport="streamable_http",
                url="https://example.com/mcp",
            ),
            secret_metadata=(),
            key_version=None,
            config_version=1,
            created_by="usr_admin_1",
            created_at=NOW,
            updated_at=NOW,
            tools_discovered_at=NOW,
        )
        return McpServerPage(items=(server,), total=1)

    async def set_requires_approval(
        self, actor: AuthContext, tool_id: str, requires_approval: bool
    ) -> AdminToolDefinition:
        raise NotImplementedError

    async def list_public(self, *, offset: int, limit: int) -> ToolPage[object]:
        raise NotImplementedError

    async def list_server_admin(
        self, server_id: str, *, offset: int, limit: int
    ) -> ToolPage[AdminToolDefinition]:
        raise NotImplementedError

    async def get_public_tool(self, tool_id: str) -> object | None:
        raise NotImplementedError

    async def get_admin_tool(self, tool_id: str) -> AdminToolDefinition | None:
        raise NotImplementedError


class _InMemorySubagentRepo:
    def __init__(self) -> None:
        self.items: dict[str, SubagentProfile] = {}

    async def create(self, actor: AuthContext, profile: SubagentProfile) -> SubagentProfile:
        del actor
        self.items[profile.id] = profile
        return profile

    async def get(self, subagent_id: str) -> SubagentProfile | None:
        item = self.items.get(subagent_id)
        if item is None or item.deleted_at is not None:
            return None
        return item

    async def get_by_slug(self, slug: str) -> SubagentProfile | None:
        for item in self.items.values():
            if item.slug == slug and item.deleted_at is None:
                return item
        return None

    async def list_page(
        self, *, offset: int, limit: int, enabled: bool | None = None
    ) -> SubagentPage:
        filtered = [
            item
            for item in self.items.values()
            if item.deleted_at is None and (enabled is None or item.enabled is enabled)
        ]
        return SubagentPage(
            items=tuple(filtered[offset : offset + limit]),
            total=len(filtered),
            offset=offset,
            limit=limit,
        )

    async def list_enabled(self) -> tuple[SubagentProfile, ...]:
        return tuple(
            item for item in self.items.values() if item.deleted_at is None and item.enabled
        )

    async def update(
        self,
        actor: AuthContext,
        subagent_id: str,
        *,
        expected_version: int,
        patch: SubagentUpdatePatch,
    ) -> SubagentProfile:
        del actor, expected_version
        current = self.items[subagent_id]
        updated = SubagentProfile(
            id=current.id,
            slug=patch.slug,
            name=patch.name,
            description=patch.description,
            system_prompt=patch.system_prompt,
            model_profile_id=patch.model_profile_id,
            allowed_tool_ids=patch.allowed_tool_ids,
            enabled=current.enabled,
            max_llm_requests=patch.max_llm_requests,
            timeout_seconds=patch.timeout_seconds,
            version=current.version + 1,
            created_by=current.created_by,
            deleted_at=current.deleted_at,
            created_at=current.created_at,
            updated_at=patch.updated_at,
        )
        self.items[subagent_id] = updated
        return updated

    async def set_enabled(
        self,
        actor: AuthContext,
        subagent_id: str,
        enabled: bool,
        *,
        updated_at: datetime,
    ) -> SubagentProfile:
        del actor
        current = self.items[subagent_id]
        updated = SubagentProfile(
            id=current.id,
            slug=current.slug,
            name=current.name,
            description=current.description,
            system_prompt=current.system_prompt,
            model_profile_id=current.model_profile_id,
            allowed_tool_ids=current.allowed_tool_ids,
            enabled=enabled,
            max_llm_requests=current.max_llm_requests,
            timeout_seconds=current.timeout_seconds,
            version=current.version + 1,
            created_by=current.created_by,
            deleted_at=current.deleted_at,
            created_at=current.created_at,
            updated_at=updated_at,
        )
        self.items[subagent_id] = updated
        return updated

    async def soft_delete(
        self, actor: AuthContext, subagent_id: str, *, deleted_at: datetime
    ) -> None:
        del actor
        current = self.items.get(subagent_id)
        if current is None or current.deleted_at is not None:
            return
        self.items[subagent_id] = SubagentProfile(
            id=current.id,
            slug=current.slug,
            name=current.name,
            description=current.description,
            system_prompt=current.system_prompt,
            model_profile_id=current.model_profile_id,
            allowed_tool_ids=current.allowed_tool_ids,
            enabled=current.enabled,
            max_llm_requests=current.max_llm_requests,
            timeout_seconds=current.timeout_seconds,
            version=current.version,
            created_by=current.created_by,
            deleted_at=deleted_at,
            created_at=current.created_at,
            updated_at=deleted_at,
        )


def test_domain_slug_validation_and_reserved_words() -> None:
    assert validate_subagent_slug("  research_agent-01  ") == "research_agent-01"

    for invalid in ("ab", "A123", "-abc", "abc-", "a" * 65, "has space"):
        with pytest.raises(SubagentValidationError):
            validate_subagent_slug(invalid)

    for reserved in RESERVED_SUBAGENT_SLUGS:
        with pytest.raises(SubagentSlugReservedError):
            validate_subagent_slug(reserved)


def test_domain_text_and_numeric_validations() -> None:
    assert validate_subagent_name("  代码审查助手  ") == "代码审查助手"
    with pytest.raises(SubagentValidationError):
        validate_subagent_name("   ")
    with pytest.raises(SubagentValidationError):
        validate_subagent_name("bad\nname")
    with pytest.raises(SubagentValidationError):
        validate_subagent_name("x" * 101)

    assert validate_subagent_description("  第一行\n第二行  ") == "第一行\n第二行"
    with pytest.raises(SubagentValidationError):
        validate_subagent_description("bad\x00desc")

    assert validate_subagent_system_prompt("  你是助手\t请简洁回答\n ") == "你是助手\t请简洁回答"
    with pytest.raises(SubagentValidationError):
        validate_subagent_system_prompt("bad\x07prompt")

    assert validate_subagent_max_llm_requests(None) is None
    assert validate_subagent_max_llm_requests(1) == 1
    assert validate_subagent_max_llm_requests(50) == 50
    for bad_req in (0, 51, True):
        with pytest.raises(SubagentValidationError):
            validate_subagent_max_llm_requests(bad_req)

    assert validate_subagent_timeout_seconds(None) is None
    assert validate_subagent_timeout_seconds(10) == 10
    assert validate_subagent_timeout_seconds(1800) == 1800
    for bad_timeout in (9, 1801, False):
        with pytest.raises(SubagentValidationError):
            validate_subagent_timeout_seconds(bad_timeout)


def test_domain_allowed_tools_dedup_and_forbid_dispatch() -> None:
    deduped = validate_subagent_allowed_tool_ids(
        ["builtin_knowledge_search", "tool_a", "builtin_knowledge_search"]
    )
    assert deduped == ("builtin_knowledge_search", "tool_a")

    with pytest.raises(SubagentToolForbiddenError):
        validate_subagent_allowed_tool_ids(["builtin_knowledge_search", DISPATCH_SUBAGENTS_TOOL_ID])

    with pytest.raises(SubagentValidationError):
        validate_subagent_allowed_tool_ids([f"tool_{i}" for i in range(101)])


def test_service_authorization_and_external_checks() -> None:
    async def scenario() -> None:
        actor = _actor(UserRole.ADMIN)
        repo = _InMemorySubagentRepo()
        mcp_tool = AdminToolDefinition(
            tool_id="tool_mcp_1",
            model_name="mcp_github_search",
            description="Search GitHub",
            input_schema={"type": "object"},
            server_id="mcp_1",
            remote_name="search",
            requires_approval=False,
        )
        catalog = DefaultSubagentToolCatalog(_StubMcpRepo((mcp_tool,)))  # type: ignore[arg-type]
        service = SubagentService(
            repo,
            _StubAuthReader(actor.user),
            model_checker=_StubModelChecker({"mp_enabled"}),
            tool_catalog=catalog,
            id_factory=lambda prefix: f"{prefix}_001",
            clock=lambda: NOW,
        )

        # 非管理员 / 失效会话拦截
        non_admin_service = SubagentService(
            repo,
            _StubAuthReader(_actor(UserRole.USER).user),
        )
        with pytest.raises(SubagentAdminRequiredError):
            await non_admin_service.list_subagents(actor)

        expired_service = SubagentService(repo, _StubAuthReader(None))
        with pytest.raises(SubagentInvalidSessionError):
            await expired_service.list_subagents(actor)

        # 模型不可用
        with pytest.raises(SubagentModelUnavailableError):
            await service.create_subagent(
                actor,
                CreateSubagentCommand(
                    slug="code-reviewer",
                    name="代码审查",
                    description="负责审查 PR",
                    system_prompt="你是资深审查员",
                    model_profile_id="mp_disabled",
                ),
            )

        # 未知工具 ID
        with pytest.raises(SubagentValidationError, match="subagent_tool_not_found"):
            await service.create_subagent(
                actor,
                CreateSubagentCommand(
                    slug="code-reviewer",
                    name="代码审查",
                    description="负责审查 PR",
                    system_prompt="你是资深审查员",
                    allowed_tool_ids=("unknown_tool_xyz",),
                ),
            )

        # 可选工具目录聚合排除 dispatch_subagents
        groups = await service.list_available_tools(actor)
        all_tool_ids = {item.tool_id for group in groups for item in group.items}
        assert DISPATCH_SUBAGENTS_TOOL_ID not in all_tool_ids
        assert "builtin_knowledge_search" in all_tool_ids
        assert "tool_mcp_1" in all_tool_ids

        # 正常创建 + 部分更新（清空 model_profile_id）
        created = await service.create_subagent(
            actor,
            CreateSubagentCommand(
                slug="code-reviewer",
                name="代码审查",
                description="负责审查 PR",
                system_prompt="你是资深审查员",
                model_profile_id="mp_enabled",
                allowed_tool_ids=("builtin_knowledge_search", "tool_mcp_1"),
                max_llm_requests=10,
                timeout_seconds=120,
            ),
        )
        assert created.id == "subagent_001"
        assert created.version == 1

        updated = await service.update_subagent(
            actor,
            created.id,
            UpdateSubagentCommand(
                expected_version=1,
                name="代码审查 V2",
                model_profile_id=None,
            ),
        )
        assert updated.version == 2
        assert updated.name == "代码审查 V2"
        assert updated.model_profile_id is None
        assert updated.allowed_tool_ids == ("builtin_knowledge_search", "tool_mcp_1")

        with pytest.raises(SubagentNotFoundError):
            await service.get_subagent(actor, "subagent_missing")

    asyncio.run(scenario())
