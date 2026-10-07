"""工具管理器测试."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from athena.core.tools.tool_definitions import MCPTool, NativeTool
from athena.core.tools.mcp.client import MCPClient
from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.spec import ToolSpec, get_tool_context
from athena.models.tool import RiskLevel, ToolExecutionMode
from tests.fakes import make_tool_manager


@pytest.fixture
def manager() -> UnifiedToolManager:
    m = make_tool_manager()
    register_builtin_tools(m)
    return m


def test_builtin_tools_registered(manager: UnifiedToolManager):
    names = manager.list_names()
    assert "read_local_file" in names
    assert "write_file" in names
    assert "list_directory" in names
    assert "exec_shell" in names


def test_tool_risk_levels(manager: UnifiedToolManager):
    assert manager.get_risk_level("read_local_file") == "low"
    assert manager.get_risk_level("write_file") == "medium"
    assert manager.get_risk_level("exec_shell") == "high"


def test_mcp_client_requires_exactly_one_transport():
    with pytest.raises(ValueError, match="exactly one"):
        MCPClient()
    with pytest.raises(ValueError, match="exactly one"):
        MCPClient(server_command=["python"], server_url="http://localhost")


def test_require_approval_flags(manager: UnifiedToolManager):
    assert manager.require_approval("read_local_file") is False
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
    assert params.properties["path"].type == "string"
    assert "path" in params.required
    assert "encoding" not in params.required


def test_native_tool_schema_resolves_postponed_boolean_annotation():
    """启用 postponed annotations 时，布尔参数仍应生成 boolean schema。"""

    async def sample_tool(include_hidden: bool = False) -> str:
        return str(include_hidden)

    tool = NativeTool(
        name="sample_boolean",
        description="sample boolean tool",
        handler=sample_tool,
    )

    assert tool.schema.parameters.properties["include_hidden"].type == "boolean"


@pytest.mark.asyncio
async def test_call_unregistered_tool():
    m = make_tool_manager()
    result = await m.call_tool(
        "nonexistent", {}, session_id="s", run_id="r", tool_call_id="tc"
    )
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


@pytest.mark.asyncio
async def test_tool_call_repository_persists_step_link(postgres_url):
    """工具账本应在步骤表存在时成功写入并保留归属。"""
    from datetime import datetime

    from athena.infrastructure.postgre.database import Database
    from athena.models.tool import ToolCallRecord, ToolCallStatus

    database = Database(postgres_url)
    await database.connect()
    await database.sessions.create("session-1", "Tools")
    await database.tool_calls.save(
        ToolCallRecord(
            id="tool-call-1",
            session_id="session-1",
            run_id="run-1",
            step_id="step-1",
            step_number=1,
            tool_name="read_local_file",
            status=ToolCallStatus.RUNNING,
            started_at=datetime.now(),
        )
    )

    record = await database.tool_calls.get("tool-call-1")
    assert record is not None
    assert record.step_id == "step-1"
    await database.close()


@pytest.mark.asyncio
async def test_tool_call_repository_concurrent_save_and_claim(postgres_url):
    """并行工具调用写入和领取不应依赖 BEGIN IMMEDIATE。"""
    from datetime import datetime

    from athena.infrastructure.postgre.database import Database
    from athena.models.tool import ToolCallRecord, ToolCallStatus

    database = Database(postgres_url)
    await database.connect()
    await database.sessions.create("session-1", "Tools")

    records = [
        ToolCallRecord(
            id=f"tool-call-{index}",
            session_id="session-1",
            run_id="run-1",
            step_id=f"step-{index}",
            step_number=index,
            tool_name="read_local_file",
            status=ToolCallStatus.PENDING,
            started_at=datetime.now(),
        )
        for index in range(20)
    ]
    await asyncio.gather(*(database.tool_calls.save(record) for record in records))

    persisted = await database.tool_calls.query("session-1")
    assert {record.id for record in persisted} == {record.id for record in records}

    claimed = await asyncio.gather(
        *(
            database.tool_calls.claim_for_execution(record.id)
            for record in records
        )
    )
    assert sum(claimed) == len(records)

    claimed_again = await asyncio.gather(
        *(
            database.tool_calls.claim_for_execution(record.id)
            for record in records
        )
    )
    assert not any(claimed_again)
    await database.close()


def test_langchain_tools_conversion(manager: UnifiedToolManager):
    tools = manager.get_langchain_tools()
    assert len(tools) == 8
    names = {t.name for t in tools}
    assert names == {
        "read_local_file",
        "write_file",
        "list_directory",
        "exec_shell",
        "get_local_file_info",
        "read_local_file_section",
        "search_local_file",
        "read_local_file_full",
    }


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


# ---------------------------------------------------------------------------
# 工具运行上下文透传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_tool_receives_coroutine_local_context():
    """原生工具通过 ToolContext 读取可信上下文，不污染模型参数。"""
    received: dict[str, Any] = {}

    async def spawn_handler(task: str) -> str:
        context = get_tool_context()
        received["session_id"] = context.session_id
        received["run_id"] = context.run_id
        received["tool_call_id"] = context.tool_call_id
        return f"done:{task}"

    m = make_tool_manager()
    m.register(
        ToolSpec(
            name="context_probe",
            description="inspect tool execution context",
            handler=spawn_handler,
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                },
                "required": ["task"],
            },
        )
    )
    result = await m.call_tool(
        "context_probe",
        {"task": "t"},
        session_id="s",
        run_id="20260809_abc",
        tool_call_id="tc-parent",
    )
    assert result.status == "success"
    assert received == {
        "session_id": "s",
        "run_id": "20260809_abc",
        "tool_call_id": "tc-parent",
    }


@pytest.mark.asyncio
async def test_worker_cannot_invoke_delegation_tool() -> None:
    """Runtime 必须拒绝 Worker 绕过 Prompt 递归派生。"""

    invoked = False

    async def handler(task: str) -> str:
        nonlocal invoked
        invoked = True
        return task

    manager = make_tool_manager()
    manager.register(
        ToolSpec(
            name="spawn_sub_agent",
            description="delegate",
            handler=handler,
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
        )
    )

    result = await manager.call_tool(
        "spawn_sub_agent",
        {"task": "nested"},
        session_id="session-1",
        run_id="worker-1",
        tool_call_id="call-1",
        agent_role="worker",
        depth=1,
    )

    assert result.status == "failed"
    assert result.error and "delegation_forbidden" in result.error
    assert invoked is False


# ---------------------------------------------------------------------------
# 文件分析工具测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_info_text(tmp_path):
    """get_file_info 应返回文本文件的元数据。"""
    from athena.core.tools.builtin.file_tools import get_file_info

    file_path = tmp_path / "test.py"
    file_path.write_text("line1\nline2\nline3\n")

    info = await get_file_info(path=str(file_path))
    assert info["size_kb"] < 1
    assert info["line_count"] == 3
    assert info["is_binary"] is False
    assert info["file_type"] == ".py"


@pytest.mark.asyncio
async def test_get_file_info_binary(tmp_path):
    """get_file_info 应检测二进制文件。"""
    from athena.core.tools.builtin.file_tools import get_file_info

    file_path = tmp_path / "test.bin"
    file_path.write_bytes(b"\x00\x01\x02\x03")

    info = await get_file_info(path=str(file_path))
    assert info["is_binary"] is True
    # 二进制文件不计算行数，返回 0
    assert info["line_count"] == 0


@pytest.mark.asyncio
async def test_get_file_info_not_found(tmp_path):
    """get_file_info 文件不存在时应抛出异常。"""
    from athena.core.tools.builtin.file_tools import get_file_info

    with pytest.raises(FileNotFoundError):
        await get_file_info(path=str(tmp_path / "nonexistent.txt"))


@pytest.mark.asyncio
async def test_read_file_section(tmp_path):
    """read_file_section 应读取指定行范围。"""
    from athena.core.tools.builtin.file_tools import read_file_section

    file_path = tmp_path / "test.txt"
    file_path.write_text("line1\nline2\nline3\nline4\nline5\n")

    content = await read_file_section(path=str(file_path), start_line=2, limit=3)
    assert "2: line2" in content
    assert "3: line3" in content
    assert "4: line4" in content
    assert "line1" not in content
    assert "line5" not in content


@pytest.mark.asyncio
async def test_read_file_section_invalid_params(tmp_path):
    """read_file_section 参数无效时应抛出异常。"""
    from athena.core.tools.builtin.file_tools import read_file_section

    file_path = tmp_path / "test.txt"
    file_path.write_text("line1\nline2\n")

    with pytest.raises(ValueError, match="start_line"):
        await read_file_section(path=str(file_path), start_line=0)

    with pytest.raises(ValueError, match="limit"):
        await read_file_section(path=str(file_path), start_line=1, limit=0)

    with pytest.raises(ValueError, match="不能超过"):
        await read_file_section(path=str(file_path), start_line=1, limit=300)


@pytest.mark.asyncio
async def test_read_file_section_out_of_range(tmp_path):
    """read_file_section 起始行超出文件范围时应返回提示。"""
    from athena.core.tools.builtin.file_tools import read_file_section

    file_path = tmp_path / "test.txt"
    file_path.write_text("line1\nline2\n")

    content = await read_file_section(path=str(file_path), start_line=10, limit=5)
    assert "无内容" in content


@pytest.mark.asyncio
async def test_search_in_file(tmp_path):
    """search_in_file 应返回匹配的行及其行号。"""
    import json as json_mod

    from athena.core.tools.builtin.file_tools import search_in_file

    file_path = tmp_path / "test.py"
    file_path.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

    result = await search_in_file(path=str(file_path), pattern=r"def \w+")
    matches = json_mod.loads(result)
    assert len(matches) == 2
    assert matches[0]["line"] == 1
    assert "def foo()" in matches[0]["content"]
    assert matches[1]["line"] == 4
    assert "def bar()" in matches[1]["content"]


@pytest.mark.asyncio
async def test_search_in_file_no_match(tmp_path):
    """search_in_file 无匹配时应返回空列表。"""
    import json as json_mod

    from athena.core.tools.builtin.file_tools import search_in_file

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world\n")

    result = await search_in_file(path=str(file_path), pattern=r"nonexistent")
    matches = json_mod.loads(result)
    assert matches == []


@pytest.mark.asyncio
async def test_search_in_file_invalid_regex(tmp_path):
    """search_in_file 无效正则时应抛出异常。"""
    from athena.core.tools.builtin.file_tools import search_in_file

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello\n")

    with pytest.raises(ValueError, match="无效的正则表达式"):
        await search_in_file(path=str(file_path), pattern=r"[invalid")


@pytest.mark.asyncio
async def test_read_full_file_small(tmp_path):
    """read_full_file 应返回小文件的全量内容。"""
    from athena.core.tools.builtin.file_tools import read_full_file

    file_path = tmp_path / "small.txt"
    content = "hello\nworld\n"
    file_path.write_text(content)

    result = await read_full_file(path=str(file_path))
    assert result == content


@pytest.mark.asyncio
async def test_read_full_file_too_large(tmp_path):
    """read_full_file 文件超过 50KB 时应抛出异常。"""
    from athena.core.tools.builtin.file_tools import read_full_file

    file_path = tmp_path / "large.txt"
    # 创建一个超过 50KB 的文件
    file_path.write_text("x" * (50 * 1024 + 1))

    with pytest.raises(ValueError, match="文件过大"):
        await read_full_file(path=str(file_path))


@pytest.mark.asyncio
async def test_read_full_file_binary(tmp_path):
    """read_full_file 二进制文件时应抛出异常。"""
    from athena.core.tools.builtin.file_tools import read_full_file

    file_path = tmp_path / "binary.bin"
    file_path.write_bytes(b"\x00\x01\x02\x03")

    with pytest.raises(ValueError, match="二进制文件"):
        await read_full_file(path=str(file_path))
