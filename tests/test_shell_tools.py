"""exec_shell 工具回归测试 — 非零退出码应判定为失败（Bug 1）.

背景：此前 exec_shell 把 [exit_code=N] 拼进输出字符串返回，非零退出码
从不抛异常，导致 NativeTool.execute 一律记 success。本测试验证修复后
非零退出 → status=failed 的完整链路。
"""

from __future__ import annotations

import pytest

from athena.core.tools.base import NativeTool
from athena.core.tools.builtin.shell_tools import exec_shell


def _make_shell_tool() -> NativeTool:
    """构建包装 exec_shell 的 NativeTool（与 registry 注册方式一致）."""
    return NativeTool(
        name="exec_shell",
        description="execute shell command",
        handler=exec_shell,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    )


@pytest.mark.asyncio
async def test_exec_shell_success_returns_output():
    result = await exec_shell("python3 -c \"print('hello')\"", timeout=10)
    assert "[exit_code=0]" in result
    assert "hello" in result


@pytest.mark.asyncio
async def test_exec_shell_nonzero_exit_raises():
    with pytest.raises(Exception) as excinfo:
        await exec_shell("python3 -c \"import sys; sys.exit(3)\"", timeout=10)
    assert "exited with code 3" in str(excinfo.value)


@pytest.mark.asyncio
async def test_exec_shell_check_false_allows_nonzero():
    """check=False 时保留旧行为：非零退出码只返回输出，不报错."""
    result = await exec_shell(
        "python3 -c \"import sys; sys.exit(3)\"", timeout=10, check=False
    )
    assert "[exit_code=3]" in result


@pytest.mark.asyncio
async def test_native_tool_reports_failed_on_nonzero_exit():
    """非零退出码经 NativeTool 包装后应为 failed（修复前是 success）."""
    tool = _make_shell_tool()
    result = await tool.execute(command="python3 -c \"import sys; sys.exit(3)\"")
    assert result.status == "failed"
    assert "exited with code 3" in (result.error or "")


@pytest.mark.asyncio
async def test_native_tool_reports_success_on_zero_exit():
    tool = _make_shell_tool()
    result = await tool.execute(command="python3 -c \"print('ok')\"")
    assert result.status == "success"
    assert "ok" in (result.output or "")


@pytest.mark.asyncio
async def test_exec_shell_timeout_raises():
    with pytest.raises(TimeoutError):
        await exec_shell("python3 -c \"import time; time.sleep(30)\"", timeout=1)
