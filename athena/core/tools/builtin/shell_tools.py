"""内置 Shell 工具 — exec_shell.

通过 subprocess 执行 Shell 命令，高风险工具，需审批。
"""

from __future__ import annotations

import asyncio
import shlex


async def exec_shell(command: str, timeout: int = 60, cwd: str | None = None) -> str:
    """执行 Shell 命令.

    Args:
        command: 要执行的命令字符串
        timeout: 超时时间（秒），默认 60
        cwd: 工作目录，默认 None（当前目录）

    Returns:
        命令输出（stdout + stderr）
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"命令执行超时（{timeout}s）: {command}")

    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""
    exit_code = proc.returncode

    parts: list[str] = []
    parts.append(f"[exit_code={exit_code}]")
    if out:
        parts.append(f"[stdout]\n{out}")
    if err:
        parts.append(f"[stderr]\n{err}")
    return "\n".join(parts)
