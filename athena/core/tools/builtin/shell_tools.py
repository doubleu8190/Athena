"""内置 Shell 工具 — exec_shell.

通过 Docker sandbox 执行 Shell 命令，高风险工具，需审批。
"""

from __future__ import annotations

from athena.core.sandbox.models import ExecutionRequest, ExecutionStatus, SandboxRunRequest
from athena.core.tools.spec import get_tool_context, get_tool_runtime


class ShellCommandError(RuntimeError):
    """Shell 命令执行失败（非零退出码）.

    错误消息以固定前缀开头，避免与 fallback 路由的子串匹配
    （error_handler.get_fallback 用 err_key in err_msg 匹配）误命中。
    """

    def __init__(self, exit_code: int, command: str, output: str) -> None:
        """

        参数：
            exit_code (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            command (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            output (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
    context = get_tool_context()
    runtime = get_tool_runtime()
    if not runtime.settings.sandbox_enabled:
        raise RuntimeError("sandbox_unavailable")
    if not runtime.settings.exec_shell_enabled:
        raise PermissionError("exec_shell is disabled")
    workspace = runtime.workspace_manager.create(context.session_id, context.run_id)
    safe_cwd = runtime.workspace_manager.resolve_cwd(workspace, cwd)
    timeout = min(timeout, runtime.settings.sandbox_max_timeout)
    handle = await runtime.sandbox_runner.ensure_run(
        SandboxRunRequest(
            run_id=context.run_id,
            session_id=context.session_id,
            workspace=workspace,
            image=runtime.settings.sandbox_shell_image,
            network_policy=runtime.settings.sandbox_default_network,
        )
    )
    result = await runtime.sandbox_runner.execute(
        ExecutionRequest(
            execution_id=context.tool_call_id,
            run_id=handle.run_id,
            image=runtime.settings.sandbox_shell_image,
            argv=["/bin/sh", "-lc", command],
            workspace=workspace,
            cwd=safe_cwd,
            timeout_seconds=timeout,
            max_output_bytes=runtime.settings.sandbox_max_output_bytes,
            network_policy=runtime.settings.sandbox_default_network,
        )
    )
    out = result.stdout
    err = result.stderr
    exit_code = result.exit_code
    if result.status == ExecutionStatus.TIMEOUT:
        raise TimeoutError(f"命令执行超时（{timeout}s）: {command}")
    if result.status == ExecutionStatus.RUNNER_UNAVAILABLE:
        raise RuntimeError("sandbox_unavailable")

    parts: list[str] = []
    parts.append(f"[exit_code={exit_code}]")
    if out:
        parts.append(f"[stdout]\n{out}")
    if err:
        parts.append(f"[stderr]\n{err}")
    output = "\n".join(parts)

    if check and (result.status != ExecutionStatus.SUCCESS or exit_code != 0):
        raise ShellCommandError(exit_code=exit_code, command=command, output=output)
    return output
