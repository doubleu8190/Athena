"""工具管理器测试."""

from __future__ import annotations

import pytest

from athena.core.tools.base import MCPTool, NativeTool
from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.manager import UnifiedToolManager
from athena.models.tool import RiskLevel, ToolExecutionMode


@pytest.fixture
def manager() -> UnifiedToolManager:
    m = UnifiedToolManager()
    register_builtin_tools(m)
    return m


def test_builtin_tools_registered(manager: UnifiedToolManager):
    names = manager.list_names()
    assert "read_file" in names
    assert "write_file" in names
    assert "list_directory" in names
    assert "exec_shell" in names


def test_tool_risk_levels(manager: UnifiedToolManager):
    assert manager.get_risk_level("read_file") == "low"
    assert manager.get_risk_level("write_file") == "medium"
    assert manager.get_risk_level("exec_shell") == "high"


def test_require_approval_flags(manager: UnifiedToolManager):
    assert manager.require_approval("read_file") is False
    assert manager.require_approval("write_file") is True
    assert manager.require_approval("exec_shell") is True


def test_native_tool_schema_inferred():
    async def sample_tool(path: str, encoding: str = "utf-8") -> str:
        return ""

    tool = NativeTool(
        name="sample",
        description="sample tool",
        handler=sample_tool,
    )
    assert tool.schema.execution_mode == ToolExecutionMode.NATIVE
    assert tool.schema.risk_level == RiskLevel.LOW
    params = tool.schema.parameters
    assert params["properties"]["path"]["type"] == "string"
    assert "path" in params["required"]
    assert "encoding" not in params.get("required", [])


@pytest.mark.asyncio
async def test_call_unregistered_tool():
    m = UnifiedToolManager()
    result = await m.call_tool("nonexistent", {})
    assert result.status == "failed"
    assert "not registered" in (result.error or "")


@pytest.mark.asyncio
async def test_read_file_tool_execution(tmp_path):
    from athena.core.tools.builtin.file_tools import read_file

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world")

    content = await read_file(path=str(file_path))
    assert content == "hello world"


@pytest.mark.asyncio
async def test_list_directory_tool(tmp_path):
    from athena.core.tools.builtin.file_tools import list_directory

    (tmp_path / "file1.txt").write_text("a")
    (tmp_path / "subdir").mkdir()

    result = await list_directory(path=str(tmp_path))
    assert "file1.txt" in result
    assert "subdir" in result


def test_langchain_tools_conversion(manager: UnifiedToolManager):
    tools = manager.get_langchain_tools()
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert names == {"read_file", "write_file", "list_directory", "exec_shell"}


class _ObjResponse:
    """模拟 SDK 式 MCP 响应对象（带 content / isError 属性）."""

    def __init__(self, content: object, is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class _FakeMCPClient:
    """返回预设结果的假 MCP 客户端."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        return self._result


def _make_mcp_tool(client: _FakeMCPClient) -> MCPTool:
    return MCPTool(
        name="remote",
        description="远程工具",
        parameters={"type": "object", "properties": {}},
        mcp_client=client,
        server_name="srv",
        remote_name="remote",
    )


@pytest.mark.asyncio
async def test_mcp_tool_object_response_text_blocks():
    """对象式响应：内容块列表应拼接为干净文本，而非 Python repr."""
    client = _FakeMCPClient(
        _ObjResponse(
            content=[
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ]
        )
    )
    result = await _make_mcp_tool(client).execute()
    assert result.status == "success"
    assert result.output == "hello\nworld"


@pytest.mark.asyncio
async def test_mcp_tool_object_response_error():
    """对象式响应：isError=True 时走失败路径并带出文本内容."""
    client = _FakeMCPClient(
        _ObjResponse(content=[{"type": "text", "text": "boom"}], is_error=True)
    )
    result = await _make_mcp_tool(client).execute()
    assert result.status == "failed"
    assert result.error == "boom"
