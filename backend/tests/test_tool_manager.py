"""ToolManager 授权顺序与发现写入编排测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.tool import ToolManager
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.mcp import (
    DiscoveredMcpTool,
    McpAdminRequiredError,
    McpConfigVersionConflictError,
    McpConnectionInput,
    McpInvalidSessionError,
    McpServer,
    McpServerPage,
    McpServerWrite,
    StdioConfig,
)
from domain.tool import (
    AdminToolDefinition,
    PublicToolDefinition,
    ToolDefinition,
    ToolPage,
)

_NOW = datetime(2026, 11, 6, tzinfo=UTC)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def _user(role: UserRole = UserRole.ADMIN) -> User:
    return User(
        id="usr_manager",
        email="manager@example.test",
        password_hash="hash",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        role=role,
    )


class FakeAuth:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.calls: list[tuple[str, str, datetime]] = []

    async def get_current_session_user(
        self, user_id: str, session_id: str, now: datetime
    ) -> User | None:
        self.calls.append((user_id, session_id, now))
        return self.user


class FakeDiscovery:
    def __init__(self, tools: tuple[DiscoveredMcpTool, ...] = ()) -> None:
        self.tools = tools
        self.fail = False
        self.calls: list[McpConnectionInput] = []

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        self.calls.append(connection)
        if self.fail:
            raise RuntimeError("mcp_discovery_protocol_failed")
        return self.tools


class FakeServers:
    def __init__(self) -> None:
        self.read_count = 0
        self.create_calls: list[tuple[Any, McpServerWrite, tuple[ToolDefinition, ...]]] = []
        self.replace_calls: list[
            tuple[Any, str, int, McpServerWrite, tuple[ToolDefinition, ...]]
        ] = []
        self.delete_calls: list[str] = []
        self.policy_calls: list[tuple[str, bool]] = []
        self.server: McpServer | None = None
        self.raise_conflict = False

    async def create_with_tools(
        self, actor: AuthContext, server: McpServerWrite, tools: Sequence[ToolDefinition]
    ) -> McpServer:
        self.create_calls.append((actor, server, tuple(tools)))
        self.server = _server(server.id, server.name, 1)
        return self.server

    async def replace_with_tools(
        self,
        actor: AuthContext,
        server_id: str,
        expected_config_version: int,
        replacement: McpServerWrite,
        tools: Sequence[ToolDefinition],
    ) -> McpServer:
        self.replace_calls.append(
            (actor, server_id, expected_config_version, replacement, tuple(tools))
        )
        if self.raise_conflict:
            raise McpConfigVersionConflictError("mcp_config_version_conflict")
        self.server = _server(server_id, replacement.name, expected_config_version + 1)
        return self.server

    async def delete(self, actor: AuthContext, server_id: str) -> None:
        self.delete_calls.append(server_id)

    async def get(self, server_id: str) -> McpServer | None:
        self.read_count += 1
        return self.server if self.server is not None and self.server.id == server_id else None

    async def list_page(self, *, offset: int, limit: int) -> McpServerPage:
        self.read_count += 1
        return McpServerPage(
            (self.server,) if self.server is not None else (), int(self.server is not None)
        )

    async def set_requires_approval(
        self, actor: AuthContext, tool_id: str, requires_approval: bool
    ) -> AdminToolDefinition:
        del actor
        self.policy_calls.append((tool_id, requires_approval))
        return AdminToolDefinition(
            tool_id,
            "mcp__00000000000040008000000000000000",
            "d",
            {},
            "mcp_a",
            "remote",
            requires_approval,
        )


class FakeTools:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.admin = AdminToolDefinition(
            "tool_00000000000040008000000000000000",
            "mcp__00000000000040008000000000000000",
            "d",
            {},
            "mcp_a",
            "remote",
        )
        self.public = PublicToolDefinition(
            "tool_00000000000040008000000000000000",
            "mcp__00000000000040008000000000000000",
            "d",
            {},
        )

    async def list_public(self, *, offset: int, limit: int) -> ToolPage[PublicToolDefinition]:
        self.reads.append("public")
        return ToolPage((self.public,), 1)

    async def list_admin(self, *, offset: int, limit: int) -> ToolPage[AdminToolDefinition]:
        self.reads.append("admin")
        return ToolPage((self.admin,), 1)

    async def list_server_admin(
        self, server_id: str, *, offset: int, limit: int
    ) -> ToolPage[AdminToolDefinition]:
        self.reads.append("server_admin")
        return ToolPage((self.admin,), 1)

    async def get_public_tool(self, tool_id: str) -> PublicToolDefinition | None:
        self.reads.append("get_public")
        return self.public

    async def get_admin_tool(self, tool_id: str) -> AdminToolDefinition | None:
        self.reads.append("get_admin")
        return self.admin


def _server(server_id: str, name: str, version: int) -> McpServer:
    return McpServer(
        id=server_id,
        name=name,
        config=StdioConfig("stdio", "/bin/echo"),
        secret_metadata=(),
        key_version=None,
        config_version=version,
        created_by="usr_manager",
        created_at=_NOW,
        updated_at=_NOW,
        tools_discovered_at=_NOW,
    )


def _manager(
    *, role: UserRole | None = UserRole.ADMIN, discovery: FakeDiscovery | None = None
) -> tuple[ToolManager, FakeAuth, FakeServers, FakeTools, FakeDiscovery]:
    auth = FakeAuth(None if role is None else _user(role))
    servers = FakeServers()
    tools = FakeTools()
    found: FakeDiscovery = discovery or FakeDiscovery(
        (DiscoveredMcpTool("remote", "desc", {"type": "object"}),)
    )
    tool_ids = iter(
        [
            "tool_00000000000040008000000000000001",
            "tool_00000000000040008000000000000002",
            "tool_00000000000040008000000000000003",
        ]
    )

    def create_id(prefix: str) -> str:
        return "mcp_" + "a" * 32 if prefix == "mcp" else next(tool_ids)

    servers.list_public = tools.list_public  # type: ignore[attr-defined]
    servers.list_admin = tools.list_admin  # type: ignore[attr-defined]
    servers.list_server_admin = tools.list_server_admin  # type: ignore[attr-defined]
    servers.get_public_tool = tools.get_public_tool  # type: ignore[attr-defined]
    servers.get_admin_tool = tools.get_admin_tool  # type: ignore[attr-defined]
    manager = ToolManager(
        cast(Any, servers),
        found,
        auth,
        id_factory=create_id,
        clock=lambda: _NOW,
    )
    return manager, auth, servers, tools, found


def test_invalid_session_or_non_admin_is_rejected_before_resource_reads() -> None:
    actor = AuthContext(_user(UserRole.USER), "ses_user")
    manager, _, servers, _, discovery = _manager(role=None)
    with pytest.raises(McpInvalidSessionError, match="mcp_session_invalid"):
        _run(manager.get_mcp_server(actor, "missing"))
    with pytest.raises(McpInvalidSessionError):
        _run(manager.register_mcp(actor, "name", StdioConfig("stdio", "/bin/echo"), {}))
    assert servers.read_count == 0
    assert discovery.calls == []

    manager, _, servers, _, discovery = _manager(role=UserRole.USER)
    with pytest.raises(McpAdminRequiredError, match="mcp_admin_required"):
        _run(manager.list_mcp_servers(actor))
    with pytest.raises(McpAdminRequiredError):
        _run(manager.register_mcp(actor, "name", StdioConfig("stdio", "/bin/echo"), {}))
    assert servers.read_count == 0
    assert discovery.calls == []


def test_register_failure_does_not_write_and_success_maps_ids_to_model_names() -> None:
    actor = AuthContext(_user(), "ses_admin")
    failing = FakeDiscovery()
    failing.fail = True
    manager, _, servers, _, _ = _manager(discovery=failing)
    with pytest.raises(RuntimeError):
        _run(manager.register_mcp(actor, "Name", StdioConfig("stdio", "/bin/echo"), {}))
    assert servers.create_calls == []

    manager, _, servers, _, _ = _manager()
    created = _run(manager.register_mcp(actor, "Name", StdioConfig("stdio", "/bin/echo"), {}))
    assert created.id == "mcp_" + "a" * 32
    saved = servers.create_calls[0][2][0]
    assert saved.tool_id == "tool_00000000000040008000000000000001"
    assert saved.model_name == "mcp__00000000000040008000000000000001"


def test_update_failure_preserves_catalog_and_version_conflict_propagates() -> None:
    actor = AuthContext(_user(), "ses_admin")
    manager, _, servers, _, discovery = _manager()
    servers.server = _server("mcp_old", "Old", 4)
    discovery.fail = True
    with pytest.raises(RuntimeError):
        _run(manager.update_mcp(actor, "mcp_old", 4, "New", StdioConfig("stdio", "/bin/echo"), {}))
    assert servers.replace_calls == []

    discovery.fail = False
    servers.raise_conflict = True
    with pytest.raises(McpConfigVersionConflictError):
        _run(manager.update_mcp(actor, "mcp_old", 4, "New", StdioConfig("stdio", "/bin/echo"), {}))
    assert len(servers.replace_calls) == 1


def test_reads_authorize_first_and_user_tool_lists_never_discover() -> None:
    actor = AuthContext(_user(UserRole.USER), "ses_user")
    manager, _, servers, tools, discovery = _manager(role=UserRole.USER)
    public = _run(manager.list_public_tools(actor, limit=1))
    assert public.items[0] == tools.public
    assert not hasattr(public.items[0], "server_id")
    assert discovery.calls == []
    assert tools.reads == ["public"]
    with pytest.raises(ValueError, match="mcp_page_invalid"):
        _run(manager.list_public_tools(actor, limit=101))

    manager, _, servers, tools, _ = _manager(role=UserRole.USER)
    with pytest.raises(McpAdminRequiredError):
        _run(manager.list_admin_tools(actor))
    assert tools.reads == []
    assert servers.read_count == 0


def test_another_admin_can_manage_global_catalog() -> None:
    actor = AuthContext(_user(UserRole.ADMIN), "ses_admin")
    second_admin = AuthContext(replace(_user(UserRole.ADMIN), id="usr_other_admin"), "ses_other")
    manager, auth, servers, tools, _ = _manager(role=UserRole.ADMIN)
    servers.server = _server("mcp_global", "Global", 1)
    assert _run(manager.list_mcp_servers(second_admin)).items[0].id == "mcp_global"
    assert _run(manager.get_mcp_server(second_admin, "mcp_global")).id == "mcp_global"
    assert _run(manager.list_admin_tools(second_admin)).items[0] == tools.admin
    assert all(call[0] in {actor.user.id, second_admin.user.id} for call in auth.calls)


def test_only_admin_can_update_approval_policy_with_a_real_bool() -> None:
    user_actor = AuthContext(_user(UserRole.USER), "ses_user")
    manager, _, servers, _, _ = _manager(role=UserRole.USER)
    with pytest.raises(McpAdminRequiredError):
        _run(manager.update_tool_approval_policy(user_actor, "tool_x", True))
    assert servers.policy_calls == []

    admin_actor = AuthContext(_user(), "ses_admin")
    manager, _, servers, _, _ = _manager()
    with pytest.raises(ValueError, match="mcp_tool_policy_invalid"):
        _run(manager.update_tool_approval_policy(admin_actor, "tool_x", 1))  # type: ignore[arg-type]
    result = _run(manager.update_tool_approval_policy(admin_actor, "tool_x", True))
    assert result.requires_approval is True
    assert servers.policy_calls == [("tool_x", True)]
