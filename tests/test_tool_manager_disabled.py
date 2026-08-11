"""UnifiedToolManager 工具启停测试."""

from __future__ import annotations

import pytest

from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.manager import UnifiedToolManager


@pytest.fixture
def manager() -> UnifiedToolManager:
    m = UnifiedToolManager()
    register_builtin_tools(m)
    return m


def test_tools_enabled_by_default(manager: UnifiedToolManager):
    assert manager.is_enabled("read_file") is True
    assert manager.is_enabled("exec_shell") is True


def test_set_enabled_disable_enable(manager: UnifiedToolManager):
    assert manager.set_enabled("read_file", False) is True
    assert manager.is_enabled("read_file") is False
    assert manager.set_enabled("read_file", True) is True
    assert manager.is_enabled("read_file") is True


def test_set_enabled_unknown_tool(manager: UnifiedToolManager):
    assert manager.set_enabled("does_not_exist", False) is False
    assert manager.set_enabled("does_not_exist", True) is False


def test_list_tools_unfiltered(manager: UnifiedToolManager):
    """管理页需要看到全部工具（含停用的）."""
    manager.set_enabled("write_file", False)
    names = manager.list_names()
    assert "write_file" in names
    assert len(manager.list_tools()) == len(names)


def test_get_langchain_tools_excludes_disabled(manager: UnifiedToolManager):
    manager.set_enabled("write_file", False)
    lc = manager.get_langchain_tools()
    lc_names = {t.name for t in lc}
    assert "write_file" not in lc_names
    assert "read_file" in lc_names
    assert "exec_shell" in lc_names


@pytest.mark.asyncio
async def test_call_tool_disabled_fails(manager: UnifiedToolManager):
    manager.set_enabled("exec_shell", False)
    result = await manager.call_tool(
        "exec_shell",
        {"command": "echo hi"},
        session_id="s",
        run_id="r",
        tool_call_id="tc1",
    )
    assert result.status == "failed"
    assert "disabled" in (result.error or "")


@pytest.mark.asyncio
async def test_call_tool_reenabled_works(manager: UnifiedToolManager):
    manager.set_enabled("exec_shell", False)
    manager.set_enabled("exec_shell", True)
    result = await manager.call_tool(
        "exec_shell",
        {"command": "echo hi"},
        session_id="s",
        run_id="r",
        tool_call_id="tc2",
    )
    assert result.status == "success"
