"""内置 Shell 工具 — exec_shell.

通过 subprocess 执行 Shell 命令，高风险工具，需审批。
"""

from __future__ import annotations

import asyncio
import shlex


class ShellCommandError(RuntimeError):
    """Shell 命令执行失败（非零退出码）.

    错误消息以固定前缀开头，避免与 fallback 路由的子串匹配
    （error_handler.get_fallback 用 err_key in err_msg 匹配）误命中。
    """

    def __init__(self, exit_code: int, command: str, output: str) -> None:
        """初始化当前对象。

        参数：
            exit_code (int): 输入参数；其类型和取值约束由方法签名及实现定义。
            command (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            output (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self.exit_code = exit_code
        self.command = command
        self.output = output
        super().__init__(f"exec_shell exited with code {exit_code}: {output}")


async def exec_shell(
    command: str, timeout: int = 60, cwd: str | None = None, check: bool = True
) -> str:
    """执行 Shell 命令.

    参数：
        command: 要执行的命令字符串
        timeout: 超时时间（秒），默认 60
        cwd: 工作目录，默认 None（当前目录）
        check: 非零退出码是否视为失败（默认 True，抛 ShellCommandError；
            False 时仅返回输出，不因退出码报错）

    返回值：
        命令输出（stdout + stderr）

    异常：
        TimeoutError: 命令超时
        ShellCommandError: 退出码非零且 check=True
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"命令执行超时（{timeout}s）: {command}")
    finally:
        # 任务被取消（停止/关停）时 communicate 会被取消但子进程仍在跑，必须清理
        if proc.returncode is None:
            proc.kill()

    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""
    exit_code = proc.returncode

    parts: list[str] = []
    parts.append(f"[exit_code={exit_code}]")
    if out:
        parts.append(f"[stdout]\n{out}")
    if err:
        parts.append(f"[stderr]\n{err}")
    output = "\n".join(parts)

    if check and exit_code != 0:
        raise ShellCommandError(exit_code=exit_code, command=command, output=output)
    return output
