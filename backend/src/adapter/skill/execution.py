"""执行登记 Python 脚本的独立进程适配器。

执行器只是进程与工作目录协议，不构成沙箱，也不给脚本增加 CPU、内存、磁盘、联网、
单次时长或调用次数限制。Run 取消、失租和截止时间由调用层的异步取消触发 ``SIGKILL``。
进程组信号只覆盖仍属于启动会话的进程；主动脱离进程组的后代，以及 Worker 被强杀或
宿主崩溃时，无法由本执行器的 ``finally`` 保证回收。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import stat
import sys
from collections.abc import AsyncGenerator, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from common.async_utils import await_task_shielded
from common.json import dumps
from common.safe_path import (
    reject_existing_symlink_components,
    safe_relative_posix_parts,
)
from domain.skill.execution import (
    JsonValue,
    ScriptCleanupErrorCode,
    ScriptExecutionCancelled,
    ScriptExecutionConfigurationError,
    ScriptExecutionErrorCode,
    ScriptExecutionPort,
    ScriptExecutionRequest,
    ScriptExecutionResult,
    ScriptManifestOutput,
)

logger = logging.getLogger(__name__)

MAX_SCRIPT_LOG_BYTES = 5 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PIPE_DRAIN_AFTER_STOP_SECONDS = 0.2
_PROCESS_EXIT_POLL_SECONDS = 0.05
_DIRECT_PROCESS_CLEANUP_SECONDS = 0.5
_MAX_MANIFEST_BYTES = 1024 * 1024
_MANIFEST_PATH_MAX_CHARACTERS = 512
_MANIFEST_LABEL_MAX_CHARACTERS = 160
_SCRIPT_HOME_DIRECTORY = ".athena-home"
_SCRIPT_TMP_DIRECTORY = ".athena-tmp"
# 仅透传 POSIX/venv 正常运行所需的最小宿主环境变量，彻底阻断 ATHENA_* 密钥与 PYTHON* 污染。
_ALLOWED_HOST_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    """经过路径约束校验且可直接用于启动脚本的执行目录。"""

    working_directory: Path
    script_path: Path
    input_path: Path
    input_files: tuple[tuple[str, Path], ...]
    output_directory: Path
    home_directory: Path
    temp_directory: Path


class _LogCollector:
    """并发保留 stdout/stderr，并对两路合计执行固定字节上限。"""

    def __init__(self, maximum_bytes: int = MAX_SCRIPT_LOG_BYTES) -> None:
        if maximum_bytes <= 0:
            raise ValueError("script_log_limit_invalid")
        self._remaining_bytes = maximum_bytes
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False

    def add(self, stream_name: str, chunk: bytes) -> None:
        """保留未超限前缀并标记截断，调用方仍持续排空管道以防子进程阻塞。"""
        if stream_name not in {"stdout", "stderr"}:
            raise ValueError("script_log_stream_invalid")
        retained_length = min(len(chunk), self._remaining_bytes)
        if retained_length:
            target = self.stdout if stream_name == "stdout" else self.stderr
            target.extend(chunk[:retained_length])
            self._remaining_bytes -= retained_length
        if retained_length < len(chunk):
            self.truncated = True

    def snapshot(self) -> tuple[bytes, bytes, bool]:
        """返回不可变日志快照与截断标记。"""
        return bytes(self.stdout), bytes(self.stderr), self.truncated


def _reject_json_constant(_value: str) -> NoReturn:
    """拒绝 Python json 默认放行的 NaN / Infinity 非法常量。"""
    raise ValueError("script_manifest_invalid")


async def _run_shielded_cleanup[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str,
    fallback: T,
) -> tuple[T, asyncio.CancelledError | None]:
    """在抗二次取消护盾中完成单步清理任务，返回结果及期间捕获的取消异常。"""
    task = asyncio.create_task(coro, name=name)
    cancelled = await await_task_shielded(task)
    try:
        return task.result(), cancelled
    except asyncio.CancelledError as error:
        return fallback, cancelled or error
    except Exception:
        return fallback, cancelled


class PythonScriptExecutor(ScriptExecutionPort):
    """在独立会话进程组中运行已登记 Python 入口。

    stdout/stderr 只保留合计 5 MiB 排查日志，超出后继续排空并丢弃，避免写满管道死锁。
    """

    def __init__(self, python_executable: Path = Path(sys.executable)) -> None:
        if os.name != "posix":
            raise ScriptExecutionConfigurationError("script_execution_posix_required")
        if (
            not python_executable.is_absolute()
            or not python_executable.is_file()
            or not os.access(python_executable, os.X_OK)
        ):
            raise ScriptExecutionConfigurationError("script_python_executable_invalid")
        self._python_executable = python_executable

    @asynccontextmanager
    async def execute(
        self, request: ScriptExecutionRequest
    ) -> AsyncGenerator[ScriptExecutionResult, None]:
        """启动脚本、排空有界日志、校验输出清单，并在退出上下文时统一清理目录。"""
        prepared = self._prepare_execution(request)
        process: asyncio.subprocess.Process | None = None
        group_killed = False
        log_tasks: list[asyncio.Task[None]] = []
        result = ScriptExecutionResult(
            exit_code=None,
            error_code=None,
            outputs=(),
            stdout=b"",
            stderr=b"",
            log_truncated=False,
            process_group_id=None,
        )

        try:
            # 严格按白名单构造子进程环境，阻断宿主凭据与 PYTHON* 污染。
            environment = self._build_script_environment(prepared)
            # 抗取消护盾等待子进程句柄落地，避免取消恰好发生在 fork/exec 窗口时泄露孤儿进程。
            launch_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    str(self._python_executable),
                    # -E 忽略 PYTHON*，-s 禁用户 site-packages，-B 禁写 .pyc，-X utf8 强制 UTF-8；
                    # 不加 -I 以保留 scripts/ 同包导入与 venv 第三方依赖解析。
                    "-E",
                    "-s",
                    "-B",
                    "-X",
                    "utf8",
                    str(prepared.script_path),
                    cwd=str(prepared.working_directory),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ),
                name="athena-script-process-launch",
            )
            launch_cancelled = await await_task_shielded(launch_task)
            if launch_task.cancelled():
                if launch_cancelled is None:
                    launch_cancelled = asyncio.CancelledError()
            else:
                try:
                    process = launch_task.result()
                except OSError:
                    result.error_code = ScriptExecutionErrorCode.PROCESS_START_FAILED
                except BaseException:
                    if launch_cancelled is None:
                        raise

            if process is not None:
                # start_new_session 保证直接子进程即进程组首进程，启动后立即记录 PGID 与结构化日志。
                result.process_group_id = process.pid
                logger.info(
                    "skill_process_started run_id=%s tool_call_id=%s worker_instance_id=%s "
                    "pid=%s pgid=%s",
                    request.run_id,
                    request.tool_call_id,
                    request.worker_instance_id,
                    process.pid,
                    process.pid,
                )
            if launch_cancelled is not None:
                raise launch_cancelled

            if process is not None:
                collector = _LogCollector()
                for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    if stream is not None:
                        log_tasks.append(
                            asyncio.create_task(
                                self._drain_stream(stream, stream_name, collector),
                                name=f"athena-script-{stream_name}",
                            )
                        )
                try:
                    return_code = await self._wait_for_direct_exit(process)
                except OSError:
                    result.error_code = ScriptExecutionErrorCode.PROCESS_FAILED
                else:
                    result.exit_code = return_code
                    if return_code != 0:
                        result.error_code = ScriptExecutionErrorCode.EXIT_NONZERO

                # 主入口退出后仅向启动时 PGID 发送一次 SIGKILL，清理同组残留后代。
                group_killed = True
                group_issue = self._kill_process_group(process.pid)
                self._append_cleanup_issue(result, group_issue)
                if group_issue is not None:
                    result.error_code = ScriptExecutionErrorCode.PROCESS_CLEANUP_FAILED

                pipe_issue = await self._settle_log_pipes(log_tasks)
                self._append_cleanup_issue(result, pipe_issue)
                if pipe_issue is not None and process.returncode is not None:
                    # 脱离进程组的后代仍占用管道导致排空超时，立即关闭读端避免 fd 在上传期泄漏。
                    self._append_cleanup_issue(result, self._close_process_transport(process))
                stdout, stderr, log_truncated = collector.snapshot()
                result.stdout = stdout
                result.stderr = stderr
                result.log_truncated = log_truncated

                if result.exit_code == 0 and result.error_code is None:
                    outputs, manifest_error = self._stage_manifest(prepared.output_directory)
                    result.outputs = outputs
                    result.error_code = manifest_error
            yield result
        finally:
            active_exception = sys.exc_info()[1]
            cleanup_cancelled: asyncio.CancelledError | None = None
            if process is not None and (
                not group_killed or process.returncode is None or log_tasks
            ):
                # 异常或取消路径下统一走受保护进程回收，防止残留进程组与僵尸子进程。
                cleanup_issues, cancelled_here = await _run_shielded_cleanup(
                    self._cleanup_process(process, group_killed=group_killed, tasks=log_tasks),
                    name="athena-script-process-cleanup",
                    fallback=(ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED,),
                )
                if cancelled_here is not None:
                    cleanup_cancelled = cancelled_here
                for issue in cleanup_issues:
                    self._append_cleanup_issue(result, issue)
            if process is not None:
                # 进程清理收敛后兜底关闭底层 transport 与管道读端，杜绝 fd 泄漏与 ResourceWarning。
                self._append_cleanup_issue(result, self._close_process_transport(process))

            # 在工作线程执行同步目录递归删除，并通过护盾等待清理收敛后再返回或重抛取消。
            remove_issue, cancelled_here = await _run_shielded_cleanup(
                asyncio.to_thread(self._remove_working_directory, prepared.working_directory),
                name="athena-script-directory-cleanup",
                fallback=ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED,
            )
            if cancelled_here is not None:
                cleanup_cancelled = cancelled_here
            self._append_cleanup_issue(result, remove_issue)

            process_reaped = None if process is None else process.returncode is not None
            if isinstance(active_exception, asyncio.CancelledError):
                raise ScriptExecutionCancelled(
                    tuple(result.cleanup_error_codes),
                    process_group_id=result.process_group_id,
                    process_reaped=process_reaped,
                ) from active_exception
            if cleanup_cancelled is not None and active_exception is None:
                raise ScriptExecutionCancelled(
                    tuple(result.cleanup_error_codes),
                    process_group_id=result.process_group_id,
                    process_reaped=process_reaped,
                ) from None
            if result.cleanup_error_codes and active_exception is not None:
                # 兼容上层 SkillExecutionService 从配置异常或 __notes__ 提取清理错误码的协议。
                if isinstance(active_exception, ScriptExecutionConfigurationError):
                    active_exception.cleanup_error_codes = tuple(
                        dict.fromkeys(
                            (*active_exception.cleanup_error_codes, *result.cleanup_error_codes)
                        )
                    )
                else:
                    active_exception.add_note(
                        "script_cleanup_error_codes="
                        + ",".join(issue.value for issue in result.cleanup_error_codes)
                    )

    def _prepare_execution(self, request: ScriptExecutionRequest) -> _PreparedExecution:
        """校验工作目录、脚本入口、输入文件与输出目录，并写入独占 input.json。"""
        working_directory = request.working_directory
        if not working_directory.is_absolute():
            raise ScriptExecutionConfigurationError("script_working_directory_invalid")
        try:
            working_stat = working_directory.lstat()
        except OSError as error:
            raise ScriptExecutionConfigurationError("script_working_directory_invalid") from error
        if stat.S_ISLNK(working_stat.st_mode) or not stat.S_ISDIR(working_stat.st_mode):
            raise ScriptExecutionConfigurationError("script_working_directory_invalid")
        working_root = working_directory.resolve(strict=True)

        try:
            script_path = self._relative_script_path(working_root, request.script_path)
            output_directory = self._prepare_output_directory(
                working_root,
                request.output_directory,
            )
            input_files = self._resolve_input_files(working_root, request.input_files)
            input_path = working_root / "input.json"
            self._create_input_file(
                input_path,
                parameters=request.parameters,
                input_files=input_files,
                output_directory=output_directory,
            )
            # 在工作目录内创建隔离 HOME / TMPDIR，避免子进程污染宿主并随目录一并删除。
            home_directory = self._prepare_private_directory(
                working_root,
                _SCRIPT_HOME_DIRECTORY,
                output_directory=output_directory,
            )
            temp_directory = self._prepare_private_directory(
                working_root,
                _SCRIPT_TMP_DIRECTORY,
                output_directory=output_directory,
            )
            return _PreparedExecution(
                working_directory=working_root,
                script_path=script_path,
                input_path=input_path,
                input_files=input_files,
                output_directory=output_directory,
                home_directory=home_directory,
                temp_directory=temp_directory,
            )
        except Exception as error:
            # 目录进入准备阶段即归本次执行独占；任意准备失败均先删除目录并回传清理错误码。
            cleanup_issue = self._remove_working_directory(working_root)
            if cleanup_issue is not None:
                if isinstance(error, ScriptExecutionConfigurationError):
                    error.cleanup_error_codes = tuple(
                        dict.fromkeys((*error.cleanup_error_codes, cleanup_issue))
                    )
                else:
                    raise ScriptExecutionConfigurationError(
                        "script_preparation_failed",
                        (cleanup_issue,),
                    ) from error
            raise

    @staticmethod
    def _build_script_environment(prepared: _PreparedExecution) -> dict[str, str]:
        """按严格白名单构造子进程环境变量，阻断宿主业务凭据与 PYTHON* 注入。"""
        environment: dict[str, str] = {}
        for name in _ALLOWED_HOST_ENV_NAMES:
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
        environment["HOME"] = str(prepared.home_directory)
        environment["TMPDIR"] = str(prepared.temp_directory)
        environment["ATHENA_INPUT_PATH"] = str(prepared.input_path)
        return environment

    def _prepare_private_directory(
        self,
        working_root: Path,
        name: str,
        *,
        output_directory: Path,
    ) -> Path:
        """在工作目录内独占创建 0o700 隔离运行时目录（HOME/TMPDIR）。"""
        candidate = working_root / name
        if candidate == output_directory:
            raise ScriptExecutionConfigurationError("script_output_directory_invalid")
        self._reject_existing_symlink_components(working_root, candidate)
        try:
            os.mkdir(candidate, 0o700)
        except OSError as error:
            raise ScriptExecutionConfigurationError("script_working_directory_invalid") from error
        return candidate.resolve(strict=True)

    def _relative_script_path(self, working_root: Path, script_path: str) -> Path:
        """校验登记脚本必须是工作目录内 ``scripts/*.py`` 普通文件且无 symlink 穿越。"""
        try:
            parts = safe_relative_posix_parts(script_path)
        except ValueError as error:
            raise ScriptExecutionConfigurationError("script_path_invalid") from error
        if parts[0] != "scripts" or not parts[-1].endswith(".py"):
            raise ScriptExecutionConfigurationError("script_path_invalid")
        script = working_root.joinpath(*parts)
        self._require_regular_contained_file(
            working_root,
            script,
            error_code="script_path_invalid",
        )
        return script

    def _prepare_output_directory(self, working_root: Path, output_directory: Path) -> Path:
        """校验或创建位于工作目录内的普通输出目录，拒绝越界与符号链接。"""
        candidate = (
            output_directory if output_directory.is_absolute() else working_root / output_directory
        )
        candidate = Path(os.path.abspath(candidate))
        try:
            candidate.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError("script_output_directory_invalid") from error
        self._reject_existing_symlink_components(working_root, candidate)
        try:
            candidate.mkdir(exist_ok=True)
            output_stat = candidate.lstat()
        except OSError as error:
            raise ScriptExecutionConfigurationError("script_output_directory_invalid") from error
        if stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode):
            raise ScriptExecutionConfigurationError("script_output_directory_invalid")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError("script_output_directory_invalid") from error
        return resolved

    def _resolve_input_files(
        self,
        working_root: Path,
        input_files: Mapping[str, Path],
    ) -> tuple[tuple[str, Path], ...]:
        """校验逻辑输入名及工作目录内的普通输入文件副本。"""
        if not isinstance(input_files, Mapping):
            raise ScriptExecutionConfigurationError("script_input_files_invalid")
        resolved_files: list[tuple[str, Path]] = []
        for name, path in input_files.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
                or not isinstance(path, Path)
            ):
                raise ScriptExecutionConfigurationError("script_input_files_invalid")
            candidate = path if path.is_absolute() else working_root / path
            self._require_regular_contained_file(
                working_root,
                candidate,
                error_code="script_input_files_invalid",
            )
            resolved_files.append((name, candidate))
        return tuple(resolved_files)

    def _create_input_file(
        self,
        input_path: Path,
        *,
        parameters: Mapping[str, JsonValue],
        input_files: tuple[tuple[str, Path], ...],
        output_directory: Path,
    ) -> None:
        """以 0o600 独占方式写入结构化 JSON 输入文件，实现无 shell 参数传递。"""
        if not isinstance(parameters, Mapping):
            raise ScriptExecutionConfigurationError("script_parameters_invalid")
        payload = {
            "parameters": dict(parameters),
            "input_files": {name: str(path) for name, path in input_files},
            "output_dir": str(output_directory),
        }
        try:
            encoded = dumps(payload) + "\n"
            file_descriptor = os.open(input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise ScriptExecutionConfigurationError("script_input_file_invalid") from error

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader,
        stream_name: str,
        collector: _LogCollector,
    ) -> None:
        """持续读取单路日志直到 EOF，超限后继续消费丢弃以防子进程写阻塞。"""
        while chunk := await stream.read(_READ_CHUNK_BYTES):
            collector.add(stream_name, chunk)

    async def _settle_log_pipes(
        self,
        tasks: list[asyncio.Task[None]],
    ) -> ScriptCleanupErrorCode | None:
        """短暂等待日志排空任务完成，超时则取消残留任务并汇总清理错误。"""
        if not tasks:
            return None
        _, pending = await asyncio.wait(
            tasks,
            timeout=_PIPE_DRAIN_AFTER_STOP_SECONDS,
            return_when=asyncio.ALL_COMPLETED,
        )
        issue: ScriptCleanupErrorCode | None = None
        if pending:
            for task in pending:
                task.cancel()
            issue = ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task_result in results:
            if isinstance(task_result, asyncio.CancelledError):
                continue
            if isinstance(task_result, BaseException):
                issue = ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
                break
        tasks.clear()
        return issue

    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        group_killed: bool,
        tasks: list[asyncio.Task[None]],
    ) -> tuple[ScriptCleanupErrorCode, ...]:
        """终止进程组并回退强杀、有界回收直接子进程，最后收敛日志任务。"""
        issues: list[ScriptCleanupErrorCode] = []
        group_issue: ScriptCleanupErrorCode | None = None
        if not group_killed:
            group_issue = self._kill_process_group(process.pid)
            if group_issue is not None:
                issues.append(group_issue)
        if process.returncode is None:
            if group_issue is not None:
                # 进程组发信号失败时立即回退强杀直接子进程，避免主进程滞留。
                direct_issue = self._kill_direct_process(process)
                if direct_issue is not None:
                    issues.append(direct_issue)
            wait_issue = await self._wait_direct_process_bounded(process)
            if wait_issue is not None and process.returncode is None:
                direct_issue = self._kill_direct_process(process)
                if direct_issue is not None:
                    issues.append(direct_issue)
                wait_issue = await self._wait_direct_process_bounded(process)
            if wait_issue is not None:
                issues.append(wait_issue)
        pipe_issue = await self._settle_log_pipes(tasks)
        if pipe_issue is not None:
            issues.append(pipe_issue)
        return tuple(dict.fromkeys(issues))

    @staticmethod
    def _kill_process_group(process_group_id: int) -> ScriptCleanupErrorCode | None:
        """向独立会话进程组发送一次 SIGKILL；组已退出视为清理成功。"""
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return None
        except OSError:
            return ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED
        return None

    @staticmethod
    def _kill_direct_process(process: asyncio.subprocess.Process) -> ScriptCleanupErrorCode | None:
        """使用 asyncio 子进程句柄回退强杀直接子进程。"""
        if process.returncode is not None:
            return None
        try:
            process.kill()
        except ProcessLookupError:
            return None
        except OSError:
            return ScriptCleanupErrorCode.PROCESS_WAIT_FAILED
        return None

    @staticmethod
    def _close_process_transport(
        process: asyncio.subprocess.Process,
    ) -> ScriptCleanupErrorCode | None:
        """显式关闭子进程底层传输与管道读端，避免脱离进程组后代或取消窗口遗留 fd 泄漏。"""
        transport = getattr(process, "_transport", None)
        if not isinstance(transport, asyncio.BaseTransport):
            return None
        try:
            if not transport.is_closing():
                transport.close()
        except OSError:
            return ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
        return None

    async def _wait_for_direct_exit(self, process: asyncio.subprocess.Process) -> int:
        """等待直接子进程退出；规避 CPython 在后代继承管道时 process.wait() 延迟唤醒的问题。"""
        if process.returncode is not None:
            return process.returncode
        wait_task = asyncio.create_task(process.wait(), name="athena-script-process-wait")
        try:
            while process.returncode is None:
                done, _ = await asyncio.wait(
                    {wait_task},
                    timeout=_PROCESS_EXIT_POLL_SECONDS,
                )
                if done:
                    return wait_task.result()
            return process.returncode
        finally:
            if not wait_task.done():
                wait_task.cancel()
                await await_task_shielded(wait_task)
            elif not wait_task.cancelled():
                wait_task.exception()

    async def _wait_direct_process_bounded(
        self,
        process: asyncio.subprocess.Process,
    ) -> ScriptCleanupErrorCode | None:
        """在有界时限内等待直接子进程退出并完成 reap，避免留下僵尸进程。"""
        if process.returncode is not None:
            return None
        try:
            await asyncio.wait_for(
                self._wait_for_direct_exit(process),
                timeout=_DIRECT_PROCESS_CLEANUP_SECONDS,
            )
        except (TimeoutError, OSError):
            return ScriptCleanupErrorCode.PROCESS_WAIT_FAILED
        return None

    def _stage_manifest(
        self,
        output_directory: Path,
    ) -> tuple[tuple[ScriptManifestOutput, ...], ScriptExecutionErrorCode | None]:
        """校验有界 manifest.json（≤ 1 MiB）及全部声明输出文件的路径、类型与去重约束。"""
        try:
            output_stat = output_directory.lstat()
        except FileNotFoundError:
            return (), ScriptExecutionErrorCode.OUTPUT_MISSING
        except OSError:
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID
        if stat.S_ISLNK(output_stat.st_mode):
            return (), ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
        if not stat.S_ISDIR(output_stat.st_mode):
            return (), ScriptExecutionErrorCode.OUTPUT_NOT_REGULAR

        manifest_path = output_directory / "manifest.json"
        try:
            manifest_stat = manifest_path.lstat()
        except FileNotFoundError:
            return (), ScriptExecutionErrorCode.MANIFEST_MISSING
        except OSError:
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID
        if stat.S_ISLNK(manifest_stat.st_mode):
            return (), ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
        if not stat.S_ISREG(manifest_stat.st_mode):
            return (), ScriptExecutionErrorCode.OUTPUT_NOT_REGULAR
        if manifest_stat.st_size > _MAX_MANIFEST_BYTES:
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID

        try:
            with manifest_path.open("rb") as stream:
                raw_bytes = stream.read(_MAX_MANIFEST_BYTES + 1)
            if len(raw_bytes) > _MAX_MANIFEST_BYTES:
                return (), ScriptExecutionErrorCode.MANIFEST_INVALID
            text = raw_bytes.decode("utf-8", errors="strict")
            payload = json.loads(text, parse_constant=_reject_json_constant)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID

        if not isinstance(payload, dict) or set(payload) != {"outputs"}:
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID
        raw_outputs = payload["outputs"]
        if not isinstance(raw_outputs, list):
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID

        staged_outputs: list[ScriptManifestOutput] = []
        seen_paths: set[str] = set()
        for item in raw_outputs:
            if not isinstance(item, dict) or set(item) != {"path", "label"}:
                return (), ScriptExecutionErrorCode.MANIFEST_INVALID
            path = item["path"]
            label = item["label"]
            if (
                not isinstance(path, str)
                or not isinstance(label, str)
                or len(path) > _MANIFEST_PATH_MAX_CHARACTERS
                or len(label) > _MANIFEST_LABEL_MAX_CHARACTERS
            ):
                return (), ScriptExecutionErrorCode.MANIFEST_INVALID
            normalized = self._normalize_manifest_path(path)
            if normalized is None:
                return (), ScriptExecutionErrorCode.OUTPUT_PATH_INVALID
            if not self._valid_manifest_label(label):
                return (), ScriptExecutionErrorCode.MANIFEST_INVALID

            source_path = output_directory.joinpath(*normalized.split("/"))
            # 先逐段拒绝符号链接组件再 lstat 普通文件，与 _require_regular_contained_file 保持一致。
            try:
                self._reject_existing_symlink_components(output_directory, source_path)
            except ScriptExecutionConfigurationError:
                return (), ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
            try:
                source_stat = source_path.lstat()
            except FileNotFoundError:
                return (), ScriptExecutionErrorCode.OUTPUT_MISSING
            except OSError:
                return (), ScriptExecutionErrorCode.OUTPUT_PATH_INVALID
            if stat.S_ISLNK(source_stat.st_mode):
                return (), ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
            if not stat.S_ISREG(source_stat.st_mode):
                return (), ScriptExecutionErrorCode.OUTPUT_NOT_REGULAR
            if normalized in seen_paths:
                return (), ScriptExecutionErrorCode.OUTPUT_DUPLICATE
            seen_paths.add(normalized)
            staged_outputs.append(
                ScriptManifestOutput(
                    path=normalized,
                    label=label,
                    source_path=source_path,
                    size_bytes=source_stat.st_size,
                )
            )
        return tuple(staged_outputs), None

    def _require_regular_contained_file(
        self,
        working_root: Path,
        candidate: Path,
        *,
        error_code: str,
    ) -> None:
        """确认路径位于工作目录内且为无符号链接的普通文件。"""
        resolved_candidate = candidate if candidate.is_absolute() else working_root / candidate
        try:
            resolved_candidate.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError(error_code) from error
        try:
            self._reject_existing_symlink_components(working_root, resolved_candidate)
            file_stat = resolved_candidate.lstat()
        except ScriptExecutionConfigurationError:
            raise
        except OSError as error:
            raise ScriptExecutionConfigurationError(error_code) from error
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ScriptExecutionConfigurationError(error_code)
        try:
            resolved_candidate.resolve(strict=True).relative_to(working_root)
        except (OSError, ValueError) as error:
            raise ScriptExecutionConfigurationError(error_code) from error

    def _reject_existing_symlink_components(
        self,
        working_root: Path,
        candidate: Path,
    ) -> None:
        """逐段拒绝工作目录下目标路径中已存在的符号链接组件。"""
        try:
            relative = candidate.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError(
                "script_path_outside_working_directory"
            ) from error
        reject_existing_symlink_components(
            working_root,
            relative.parts,
            error_factory=lambda reason: ScriptExecutionConfigurationError(
                "script_path_link_not_allowed"
                if reason == "safe_path_symlink_not_allowed"
                else "script_path_invalid"
            ),
        )

    def _remove_working_directory(
        self,
        working_directory: Path,
    ) -> ScriptCleanupErrorCode | None:
        """删除执行器独占工作目录；目录已不存在按成功处理。"""
        try:
            shutil.rmtree(working_directory)
        except FileNotFoundError:
            return None
        except OSError:
            return ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED
        return None

    @staticmethod
    def _normalize_manifest_path(path: str) -> str | None:
        """规范化清单输出路径，拒绝绝对路径、控制字符、反斜杠、越界段与 manifest.json 自引用。"""
        try:
            parts = safe_relative_posix_parts(path)
        except ValueError:
            return None
        normalized = "/".join(parts)
        if normalized == "manifest.json":
            return None
        return normalized

    @staticmethod
    def _valid_manifest_label(label: str) -> bool:
        """校验交付文件展示标签为非空、无控制字符的有界纯文本。"""
        return (
            bool(label)
            and len(label) <= _MANIFEST_LABEL_MAX_CHARACTERS
            and all(ord(ch) >= 32 and ord(ch) != 127 for ch in label)
        )

    @staticmethod
    def _append_cleanup_issue(
        result: ScriptExecutionResult,
        issue: ScriptCleanupErrorCode | None,
    ) -> None:
        """向执行结果追加去重后的清理错误码。"""
        if issue is not None and issue not in result.cleanup_error_codes:
            result.cleanup_error_codes.append(issue)


__all__ = ["MAX_SCRIPT_LOG_BYTES", "PythonScriptExecutor"]
