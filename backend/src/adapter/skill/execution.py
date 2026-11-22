"""执行登记 Python 脚本的独立进程适配器。

执行器只是进程与工作目录协议，不构成沙箱，也不给脚本增加 CPU、内存、磁盘、联网、
单次时长或调用次数限制。Run 取消、失租和截止时间由调用层的异步取消触发 ``SIGKILL``。
进程组信号只能覆盖仍属于启动会话的进程；主动 ``setsid``/改变进程组的后代，以及
Worker 被 ``SIGKILL`` 或宿主崩溃时，无法由本执行器的 ``finally`` 保证回收。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import stat
import sys
import threading
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TextIO, cast

from common.json import dumps
from domain.skill.execution import (
    SCRIPT_PROCESS_DIAGNOSTIC_FILENAME,
    JsonValue,
    ScriptCleanupErrorCode,
    ScriptExecutionCancelled,
    ScriptExecutionConfigurationError,
    ScriptExecutionErrorCode,
    ScriptExecutionPort,
    ScriptExecutionRequest,
    ScriptExecutionResult,
    ScriptManifestOutput,
    ScriptProcessDiagnostic,
)

logger = logging.getLogger(__name__)

MAX_SCRIPT_LOG_BYTES = 5 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PIPE_DRAIN_AFTER_STOP_SECONDS = 0.2
_MANIFEST_PATH_MAX_CHARACTERS = 512
_MANIFEST_LABEL_MAX_CHARACTERS = 160
_MANIFEST_FIELD_MAX_CHARACTERS = 64
_MANIFEST_READ_CHUNK_CHARACTERS = 64 * 1024
_PROCESS_GROUP_EXIT_SECONDS = 1.0
_DIRECT_PROCESS_CLEANUP_SECONDS = 0.5
_MANIFEST_STAGE_DATABASE = ".athena-manifest.sqlite3"
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


@dataclass(slots=True)
class _LogPipeSet:
    """执行器显式持有父端写描述符和异步读端的 stdout/stderr 管道。"""

    stdout_reader: asyncio.StreamReader
    stderr_reader: asyncio.StreamReader
    stdout_writer_fd: int
    stderr_writer_fd: int
    stdout_transport: asyncio.BaseTransport
    stderr_transport: asyncio.BaseTransport

    def close_write_ends(self) -> None:
        """关闭父进程持有的写端，避免阻止读任务看到 EOF。

        参数:
            无参数。

        返回:
            无返回值；重复调用保持幂等。

        异常:
            本方法消化已经关闭或无效描述符产生的 ``OSError``。
        """
        for attribute_name in ("stdout_writer_fd", "stderr_writer_fd"):
            file_descriptor = getattr(self, attribute_name)
            if file_descriptor < 0:
                continue
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            setattr(self, attribute_name, -1)


@dataclass(slots=True)
class _ProcessGroupConvergence:
    """记录一次进程组回收的单次信号发送与收敛确认状态。"""

    process_group_id: int
    signal_sent: bool = False
    converged: bool = False
    unreaped: bool = False
    task: asyncio.Task[ScriptCleanupErrorCode | None] | None = None


class _ManifestInvalid(ValueError):
    """清单词法或结构不符合脚本输出协议。"""

    def __init__(self) -> None:
        """构造不携带清单正文或宿主路径的内部错误。

        参数:
            无参数。

        返回:
            无返回值；实例自身只表达清单非法。

        异常:
            构造函数不主动抛出业务异常。
        """
        super().__init__("script_manifest_invalid")


class _LogCollector:
    """并发保留 stdout/stderr，并对两路合计执行固定字节上限。"""

    def __init__(self, maximum_bytes: int = MAX_SCRIPT_LOG_BYTES) -> None:
        """创建两路有界缓冲区。

        参数:
            maximum_bytes: stdout 与 stderr 合计最多保留的原始字节数。

        返回:
            无返回值。

        异常:
            ``maximum_bytes`` 非正数时抛出 ``ValueError``。
        """
        if maximum_bytes <= 0:
            raise ValueError("script_log_limit_invalid")
        self._remaining_bytes = maximum_bytes
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False

    def add(self, stream_name: str, chunk: bytes) -> None:
        """保存一条日志块并在达到总上限后只记录截断状态。

        参数:
            stream_name: ``stdout`` 或 ``stderr``。
            chunk: 本次从对应管道读到的原始字节。

        返回:
            无返回值。

        异常:
            ``stream_name`` 非法时抛出 ``ValueError``。
        """
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
        """返回不可变日志快照和截断标记。

        参数:
            无参数。

        返回:
            ``(stdout, stderr, log_truncated)`` 三元组。

        异常:
            快照方法不主动抛出业务异常。
        """
        return bytes(self.stdout), bytes(self.stderr), self.truncated


class _ManifestStageCancelled(Exception):
    """后台清单暂存线程收到取消请求。"""


class _ManifestReader:
    """以字符流增量解析仅含 ``outputs`` 数组的输出清单。"""

    def __init__(
        self,
        stream: TextIO,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        """保存严格 UTF-8 文本流。

        参数:
            stream: 已按严格 UTF-8 打开的输出清单文本流。
            cancel_requested: 后台线程周期性检查的取消回调。

        返回:
            无返回值。

        异常:
            构造函数不主动抛出业务异常。
        """
        self._stream = stream
        self._cancel_requested = cancel_requested
        self._buffer = ""
        self._buffer_offset = 0

    def iter_outputs(self) -> Iterator[tuple[str, str]]:
        """逐个返回清单声明，全程不把完整清单载入内存。

        参数:
            无参数。

        返回:
            按清单顺序排列的 ``(path, label)`` 元组。

        异常:
            清单不是唯一顶层 ``outputs`` 数组或字段类型非法时抛出
            ``_ManifestInvalid``；后台暂存取消时抛出 ``_ManifestStageCancelled``。
        """
        self._skip_whitespace()
        self._expect("{")
        self._skip_whitespace()
        if self._peek() == "}":
            raise _ManifestInvalid()
        key = self._read_string(_MANIFEST_FIELD_MAX_CHARACTERS)
        if key != "outputs":
            raise _ManifestInvalid()
        self._skip_whitespace()
        self._expect(":")
        self._skip_whitespace()
        self._expect("[")

        self._skip_whitespace()
        if self._peek() == "]":
            self._take()
        else:
            while True:
                yield self._read_output()
                self._skip_whitespace()
                separator = self._take()
                if separator == "]":
                    break
                if separator != ",":
                    raise _ManifestInvalid()
                self._skip_whitespace()
                if self._peek() == "]":
                    raise _ManifestInvalid()

        self._skip_whitespace()
        self._expect("}")
        self._skip_whitespace()
        if self._peek() is not None:
            raise _ManifestInvalid()

    def _read_output(self) -> tuple[str, str]:
        """解析一个严格输出对象并返回路径及标签。

        参数:
            无参数。

        返回:
            已确认字段完整的 ``(path, label)``。

        异常:
            对象含未知/缺失/重复字段或字段不是字符串时抛出 ``_ManifestInvalid``。
        """
        self._expect("{")
        values: dict[str, str] = {}
        self._skip_whitespace()
        if self._peek() == "}":
            raise _ManifestInvalid()
        while True:
            key = self._read_string(_MANIFEST_FIELD_MAX_CHARACTERS)
            if key not in {"path", "label"} or key in values:
                raise _ManifestInvalid()
            self._skip_whitespace()
            self._expect(":")
            self._skip_whitespace()
            maximum_characters = (
                _MANIFEST_LABEL_MAX_CHARACTERS if key == "label" else _MANIFEST_PATH_MAX_CHARACTERS
            )
            values[key] = self._read_string(maximum_characters)
            self._skip_whitespace()
            separator = self._take()
            if separator == "}":
                break
            if separator != ",":
                raise _ManifestInvalid()
            self._skip_whitespace()
        if set(values) != {"path", "label"}:
            raise _ManifestInvalid()
        return values["path"], values["label"]

    def _read_string(self, maximum_characters: int) -> str:
        """读取一个有长度边界的 JSON 字符串。

        参数:
            maximum_characters: 解码后允许的最大 Unicode 字符数。

        返回:
            解码后的字符串。

        异常:
            转义、编码或长度不符合 JSON 字符串规则时抛出 ``_ManifestInvalid``。
        """
        self._skip_whitespace()
        self._expect('"')
        encoded: list[str] = ['"']
        escaped = False
        while True:
            self._check_cancelled()
            character = self._take()
            encoded.append(character)
            if len(encoded) > maximum_characters * 6 + 2:
                raise _ManifestInvalid()
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                break
        try:
            value = json.loads("".join(encoded))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise _ManifestInvalid() from error
        if not isinstance(value, str) or len(value) > maximum_characters:
            raise _ManifestInvalid()
        return value

    def _skip_whitespace(self) -> None:
        """跳过 JSON 允许的标准空白字符。

        参数:
            无参数。

        返回:
            无返回值。

        异常:
            文本提前结束时不会跳过并抛出 ``_ManifestInvalid``，由调用方继续判断。
        """
        while True:
            self._check_cancelled()
            character = self._peek()
            if character is None or character not in " \t\r\n":
                break
            self._take()

    def _peek(self) -> str | None:
        """查看下一个字符而不消费。

        参数:
            无参数。

        返回:
            下一个字符；流结束时返回 ``None``。

        异常:
            文本流读取失败时由 ``TextIO.read`` 的底层异常传播。
        """
        self._check_cancelled()
        if self._buffer_offset >= len(self._buffer):
            self._buffer = self._stream.read(_MANIFEST_READ_CHUNK_CHARACTERS)
            self._buffer_offset = 0
            if not self._buffer:
                return None
        return self._buffer[self._buffer_offset]

    def _take(self) -> str:
        """消费并返回下一个字符。

        参数:
            无参数。

        返回:
            下一个字符。

        异常:
            流提前结束时抛出 ``_ManifestInvalid``。
        """
        character = self._peek()
        if character is None:
            raise _ManifestInvalid()
        self._buffer_offset += 1
        return character

    def _check_cancelled(self) -> None:
        """在清单扫描热点循环中及时响应后台取消。

        参数:
            无参数。

        返回:
            未取消时无返回值。

        异常:
            取消回调返回 ``True`` 时抛出 ``_ManifestStageCancelled``。
        """
        if self._cancel_requested is not None and self._cancel_requested():
            raise _ManifestStageCancelled()

    def _expect(self, expected: str) -> None:
        """要求并消费固定字符。

        参数:
            expected: 期望出现的单个字符。

        返回:
            无返回值。

        异常:
            实际字符不同或提前结束时抛出 ``_ManifestInvalid``。
        """
        if self._take() != expected:
            raise _ManifestInvalid()


class _SqliteManifestOutputs:
    """从工作目录内 SQLite 暂存表逐项读取已校验输出声明。"""

    def __init__(self, database_path: Path) -> None:
        """保存只读结果数据库路径。

        参数:
            database_path: 后台线程写入、工作原理目录内的 SQLite 文件。

        返回:
            无返回值。

        异常:
            构造函数不主动抛出业务异常。
        """
        self._database_path = database_path

    def __iter__(self) -> Iterator[ScriptManifestOutput]:
        """按 manifest 顺序逐行读取输出，不构造完整列表。

        参数:
            无参数。

        返回:
            每次调用都会重新打开只读游标的惰性迭代器。

        异常:
            暂存数据库缺失或损坏时传播 ``sqlite3.Error``。
        """
        connection = sqlite3.connect(
            f"{self._database_path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            cursor = connection.execute(
                """
                SELECT path, label, source_path, size_bytes
                FROM outputs
                ORDER BY sequence
                """
            )
            for row in cursor:
                if len(row) != 4:
                    raise sqlite3.DatabaseError("script_manifest_stage_invalid")
                yield ScriptManifestOutput(
                    path=cast(str, row[0]),
                    label=cast(str, row[1]),
                    source_path=Path(cast(str, row[2])),
                    size_bytes=cast(int, row[3]),
                )
        finally:
            connection.close()


class PythonScriptExecutor(ScriptExecutionPort):
    """在独立会话进程组中运行已登记 Python 入口。

    stdout/stderr 只做合计 5 MiB 的排查日志保留，超过后继续排空并丢弃。该限制不限制
    脚本资源；执行器不设置默认脚本超时。
    """

    def __init__(self, python_executable: Path = Path(sys.executable)) -> None:
        """保存统一平台解释器路径。

        参数:
            python_executable: 要启动的 Python 可执行文件绝对路径。

        返回:
            无返回值。

        异常:
            平台不是 POSIX 或解释器路径不是绝对可执行文件时抛出
            ``ScriptExecutionConfigurationError``。
        """
        if os.name != "posix":
            raise ScriptExecutionConfigurationError("script_execution_posix_required")
        executable = python_executable
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ScriptExecutionConfigurationError("script_python_executable_invalid")
        self._python_executable = executable

    @asynccontextmanager
    async def execute(
        self, request: ScriptExecutionRequest
    ) -> AsyncGenerator[ScriptExecutionResult, None]:
        """启动脚本并保持工作目录直到调用方完成上传。

        参数:
            request: 已准备目录、登记脚本、JSON 参数、输入副本和输出目录。

        返回:
            异步生成器上下文；进入时运行至清单校验完成，退出时清理整个工作目录。

        异常:
            请求路径或输入非法时抛出 ``ScriptExecutionConfigurationError``；
            外部取消先杀进程组并原样传播。
        """
        prepared = self._prepare_execution(request)
        process: asyncio.subprocess.Process | None = None
        process_group: _ProcessGroupConvergence | None = None
        log_pipes: _LogPipeSet | None = None
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
            # 严格使用白名单构造子进程环境，不复制宿主 os.environ，
            # 防止泄漏数据库/对象存储/MCP 凭据。
            environment = self._build_script_environment(prepared)
            log_pipes = await self._open_log_pipes()
            launch_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    str(self._python_executable),
                    # -E 忽略 PYTHON* 变量，-s 禁用用户 site-packages，
                    # -B 禁止写 .pyc，-X utf8 强制 UTF-8；
                    # 不加 -I 以保留 scripts/ 同包模块导入及 venv 第三方依赖解析。
                    "-E",
                    "-s",
                    "-B",
                    "-X",
                    "utf8",
                    str(prepared.script_path),
                    cwd=str(prepared.working_directory),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_pipes.stdout_writer_fd,
                    stderr=log_pipes.stderr_writer_fd,
                    start_new_session=True,
                ),
                name="athena-script-process-launch",
            )
            launch_cancelled: asyncio.CancelledError | None = None
            try:
                while True:
                    try:
                        process = await asyncio.shield(launch_task)
                        break
                    except asyncio.CancelledError as error:
                        launch_cancelled = error
            except OSError:
                result.error_code = ScriptExecutionErrorCode.PROCESS_START_FAILED
            if process is not None:
                log_pipes.close_write_ends()
                # start_new_session 保证直接子进程就是进程组首进程，先记录 PID 可避免
                # 主进程退出后 os.getpgid 查找失败。
                process_group = _ProcessGroupConvergence(process.pid)
                result.process_group_id = process.pid
                if not self._persist_process_diagnostic(
                    prepared.working_directory,
                    request=request,
                    process_id=process.pid,
                    process_group_id=process.pid,
                ):
                    logger.warning(
                        "skill_process_diagnostic_write_failed "
                        "run_id=%s tool_call_id=%s worker_instance_id=%s",
                        request.run_id,
                        request.tool_call_id,
                        request.worker_instance_id,
                    )
            if launch_cancelled is not None:
                raise launch_cancelled
            if process is not None:
                collector = _LogCollector()
                log_tasks = [
                    asyncio.create_task(
                        self._drain_stream(log_pipes.stdout_reader, "stdout", collector),
                        name="athena-script-stdout",
                    ),
                    asyncio.create_task(
                        self._drain_stream(log_pipes.stderr_reader, "stderr", collector),
                        name="athena-script-stderr",
                    ),
                ]
                try:
                    return_code = await process.wait()
                except OSError:
                    result.error_code = ScriptExecutionErrorCode.PROCESS_FAILED
                else:
                    result.exit_code = return_code
                    if return_code != 0:
                        result.error_code = ScriptExecutionErrorCode.EXIT_NONZERO

                process_group_issue = await self._await_process_group_convergence(process_group)
                self._append_cleanup_issue(result, process_group_issue)
                if process_group_issue is not None:
                    result.error_code = ScriptExecutionErrorCode.PROCESS_CLEANUP_FAILED
                self._append_cleanup_issue(
                    result,
                    await self._settle_log_pipes(log_pipes, log_tasks),
                )
                stdout, stderr, log_truncated = collector.snapshot()
                result.stdout = stdout
                result.stderr = stderr
                result.log_truncated = log_truncated

                if result.exit_code == 0 and result.error_code is None:
                    outputs, manifest_error = await self._stage_manifest(
                        prepared.working_directory,
                        prepared.output_directory,
                    )
                    result.outputs = outputs
                    result.error_code = manifest_error
            yield result
        finally:
            active_exception = sys.exc_info()[1]
            cleanup_cancelled: asyncio.CancelledError | None = None
            if process is not None:
                cleanup_task = asyncio.create_task(
                    self._cleanup_process(
                        process,
                        process_group,
                        log_pipes,
                        log_tasks,
                    ),
                    name="athena-script-process-cleanup",
                )
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError as error:
                        cleanup_cancelled = error
                        continue
                try:
                    cleanup_issues = cleanup_task.result()
                except asyncio.CancelledError as error:
                    cleanup_cancelled = error
                    cleanup_issues = (ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED,)
                except Exception:
                    cleanup_issues = (ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED,)
                for issue in cleanup_issues:
                    self._append_cleanup_issue(result, issue)
            if log_pipes is not None:
                log_pipes.close_write_ends()
                if not self._close_read_pipes(log_pipes):
                    self._append_cleanup_issue(
                        result,
                        ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED,
                    )
            process_group_unknown = process_group is not None and process_group.unreaped
            if process_group_unknown:
                # 进程组仍存活或无法确认收敛时保留目录；诊断 JSON 只供人工排查，
                # 不能用于自动发信号或证明脚本已经结束。
                pass
            else:
                self._append_cleanup_issue(
                    result,
                    self._remove_working_directory(prepared.working_directory),
                )
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
        """校验请求路径并写入关闭后的结构化输入文件。

        参数:
            request: 调用方提供的已准备执行请求。

        返回:
            全部路径已归一化且输入文件已写入的启动信息。

        异常:
            路径越界、目录/文件类型、输入映射或 JSON 参数非法时抛出
            ``ScriptExecutionConfigurationError``。
        """
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
            input_files = tuple(
                (name, path)
                for name, path in self._resolve_input_files(
                    working_root,
                    request.input_files,
                )
            )
            input_path = working_root / "input.json"
            self._create_input_file(
                input_path,
                parameters=request.parameters,
                input_files=input_files,
                output_directory=output_directory,
            )
            # 在独占工作目录内创建受控 HOME 与 TMPDIR，避免子进程污染宿主目录并随工作目录统一清理。
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
            # 请求进入目录准备阶段后即由本次执行独占；任何准备失败都要先删除该目录。
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
        """按严格白名单构造子进程环境变量，禁止透传宿主业务凭据与 PYTHON* 变量。"""
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
        """在工作目录内独占创建权限收紧的运行时隔离目录（HOME/TMPDIR）。"""
        candidate = working_root / name
        if candidate == output_directory:
            raise ScriptExecutionConfigurationError("script_output_directory_invalid")
        self._reject_existing_symlink_components(working_root, candidate)
        try:
            os.mkdir(candidate, 0o700)
        except OSError as error:
            raise ScriptExecutionConfigurationError("script_working_directory_invalid") from error
        return candidate.resolve(strict=True)

    def _persist_process_diagnostic(
        self,
        working_directory: Path,
        *,
        request: ScriptExecutionRequest,
        process_id: int,
        process_group_id: int,
    ) -> bool:
        """写入仅供人工排查的本地进程启动摘要。

        参数:
            working_directory: 本次执行独占的工作目录。
            request: 包含 Run、调用和 Worker 启动标识的执行请求。
            process_id: 刚启动的直接子进程 PID。
            process_group_id: 独立进程组 ID，当前等于直接子进程 PID。

        返回:
            摘要写入成功时返回 ``True``；任何文件系统或编码失败返回 ``False``。

        异常:
            本方法消化诊断写入失败，不影响脚本执行主流程。
        """
        diagnostic_path = working_directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME
        diagnostic = ScriptProcessDiagnostic(
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            worker_instance_id=request.worker_instance_id,
            process_id=process_id,
            process_group_id=process_group_id,
            recorded_at=datetime.now(UTC),
            working_directory=str(working_directory),
        )
        try:
            encoded = diagnostic.model_dump_json().encode("utf-8")
            file_descriptor = os.open(
                diagnostic_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            logger.info("skill_process_started diagnostic=%s", encoded.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return False
        return True

    def _relative_script_path(self, working_root: Path, script_path: str) -> Path:
        """校验登记入口是工作目录内的普通 Python 文件。

        参数:
            working_root: 已归一化的工作目录。
            script_path: Skill 清单登记的 POSIX 相对脚本路径。

        返回:
            工作目录内的实际脚本文件路径。

        异常:
            路径不是 ``scripts/*.py``、越界、链接或不是普通文件时抛出
            ``ScriptExecutionConfigurationError``。
        """
        relative = PurePosixPath(script_path)
        if (
            relative.is_absolute()
            or "\\" in script_path
            or "\x00" in script_path
            or not script_path.startswith("scripts/")
            or not script_path.endswith(".py")
            or any(part in {"", ".", ".."} for part in script_path.split("/"))
        ):
            raise ScriptExecutionConfigurationError("script_path_invalid")
        script = working_root.joinpath(*relative.parts)
        self._require_regular_contained_file(
            working_root,
            script,
            error_code="script_path_invalid",
        )
        return script

    def _prepare_output_directory(self, working_root: Path, output_directory: Path) -> Path:
        """校验或创建工作目录内的普通输出目录。

        参数:
            working_root: 已归一化的工作目录。
            output_directory: 平台为本次调用指定的输出目录。

        返回:
            归一化且已存在的输出目录路径。

        异常:
            路径越界、链接或目录类型非法时抛出 ``ScriptExecutionConfigurationError``。
        """
        candidate = (
            output_directory if output_directory.is_absolute() else working_root / output_directory
        )
        candidate = Path(os.path.abspath(candidate))
        if not candidate.is_absolute():
            raise ScriptExecutionConfigurationError("script_output_directory_invalid")
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
        """校验输入名称及工作目录内普通文件副本。

        参数:
            working_root: 已归一化的工作目录。
            input_files: 逻辑输入名到绝对或工作目录相对路径的映射。

        返回:
            按映射顺序排列的逻辑名和绝对路径。

        异常:
            名称为空、路径越界、链接或非普通文件时抛出
            ``ScriptExecutionConfigurationError``。
        """
        if not isinstance(input_files, Mapping):
            raise ScriptExecutionConfigurationError("script_input_files_invalid")
        resolved_files: list[tuple[str, Path]] = []
        for name, path in input_files.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
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
        """以独占方式写入脚本唯一的结构化输入文件。

        参数:
            input_path: 工作目录内的 ``input.json`` 路径。
            parameters: 已冻结且必须可编码为 JSON 的脚本参数对象。
            input_files: 逻辑输入名到工作目录内绝对路径的映射。
            output_directory: 写入输入内容的绝对输出目录。

        返回:
            无返回值；文件在返回前已关闭。

        异常:
            JSON 无法编码、文件已存在或写入失败时抛出
            ``ScriptExecutionConfigurationError``。
        """
        if not isinstance(parameters, Mapping):
            raise ScriptExecutionConfigurationError("script_parameters_invalid")
        payload = {
            "parameters": dict(parameters),
            "input_files": {name: str(path) for name, path in input_files},
            "output_dir": str(output_directory),
        }
        try:
            encoded = dumps(payload) + "\n"
            file_descriptor = os.open(
                input_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise ScriptExecutionConfigurationError("script_input_file_invalid") from error

    async def _open_log_pipes(self) -> _LogPipeSet:
        """创建由执行器显式持有的 stdout/stderr 管道与异步读流。

        参数:
            无参数。

        返回:
            两路读取流、父端写描述符和可主动关闭的读传输。

        异常:
            管道创建或 asyncio 连接失败时关闭已分配资源并抛出
            ``ScriptExecutionConfigurationError``。
        """
        stdout_read_fd = -1
        stdout_writer_fd = -1
        stderr_read_fd = -1
        stderr_writer_fd = -1
        stdout_file: BinaryIO | None = None
        stderr_file: BinaryIO | None = None
        stdout_transport: asyncio.BaseTransport | None = None
        stderr_transport: asyncio.BaseTransport | None = None
        try:
            loop = asyncio.get_running_loop()
            stdout_read_fd, stdout_writer_fd = os.pipe()
            stderr_read_fd, stderr_writer_fd = os.pipe()
            stdout_reader = asyncio.StreamReader()
            stderr_reader = asyncio.StreamReader()
            stdout_file = os.fdopen(stdout_read_fd, "rb", buffering=0)
            stdout_read_fd = -1
            stdout_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stdout_reader),
                stdout_file,
            )
            stdout_file = None
            stderr_file = os.fdopen(stderr_read_fd, "rb", buffering=0)
            stderr_read_fd = -1
            stderr_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stderr_reader),
                stderr_file,
            )
            stderr_file = None
            return _LogPipeSet(
                stdout_reader=stdout_reader,
                stderr_reader=stderr_reader,
                stdout_writer_fd=stdout_writer_fd,
                stderr_writer_fd=stderr_writer_fd,
                stdout_transport=stdout_transport,
                stderr_transport=stderr_transport,
            )
        except BaseException as error:
            if stdout_transport is not None:
                try:
                    stdout_transport.close()
                except OSError:
                    pass
            if stderr_transport is not None:
                try:
                    stderr_transport.close()
                except OSError:
                    pass
            if stdout_file is not None:
                try:
                    stdout_file.close()
                except OSError:
                    pass
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except OSError:
                    pass
            for file_descriptor in (
                stdout_read_fd,
                stdout_writer_fd,
                stderr_read_fd,
                stderr_writer_fd,
            ):
                if file_descriptor >= 0:
                    try:
                        os.close(file_descriptor)
                    except OSError:
                        pass
            if isinstance(error, asyncio.CancelledError):
                raise
            if not isinstance(error, (OSError, RuntimeError)):
                raise
            raise ScriptExecutionConfigurationError("script_log_pipe_invalid") from error

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader,
        stream_name: str,
        collector: _LogCollector,
    ) -> None:
        """持续读取单路日志，达到总上限后仍排空并丢弃。

        参数:
            stream: stdout 或 stderr 的异步读取流。
            stream_name: 用于选择接收缓冲区的 ``stdout`` 或 ``stderr``。
            collector: 两路共享的合计有界收集器。

        返回:
            无返回值；管道 EOF 或读取流被关闭后结束。

        异常:
            任务被取消时抛出 ``asyncio.CancelledError``；底层读取错误一并传播。
        """
        while chunk := await stream.read(_READ_CHUNK_BYTES):
            collector.add(stream_name, chunk)

    async def _settle_log_pipes(
        self,
        log_pipes: _LogPipeSet,
        tasks: list[asyncio.Task[None]],
    ) -> ScriptCleanupErrorCode | None:
        """等待有界日志任务并在遗留管道时主动关闭读端。

        参数:
            log_pipes: 执行器持有的两路管道。
            tasks: 已并发启动的 stdout/stderr 排空任务。

        返回:
            管道读取或关闭失败时的稳定清理错误码；否则返回 ``None``。

        异常:
            任务被外部取消时抛出 ``asyncio.CancelledError``。
        """
        if not tasks:
            return None
        done, pending = await asyncio.wait(
            tasks,
            timeout=_PIPE_DRAIN_AFTER_STOP_SECONDS,
            return_when=asyncio.ALL_COMPLETED,
        )
        del done
        issue: ScriptCleanupErrorCode | None = None
        if pending:
            if not self._close_read_pipes(log_pipes):
                issue = ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
            await asyncio.wait(
                pending,
                timeout=_PIPE_DRAIN_AFTER_STOP_SECONDS,
                return_when=asyncio.ALL_COMPLETED,
            )
        still_pending = [task for task in tasks if not task.done()]
        for task in still_pending:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task_result in results:
            if isinstance(task_result, asyncio.CancelledError):
                continue
            if isinstance(task_result, BaseException):
                issue = ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
                break
        if not self._close_read_pipes(log_pipes):
            issue = ScriptCleanupErrorCode.LOG_PIPE_CLEANUP_FAILED
        return issue

    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        process_group: _ProcessGroupConvergence | None,
        log_pipes: _LogPipeSet | None,
        tasks: list[asyncio.Task[None]],
    ) -> tuple[ScriptCleanupErrorCode, ...]:
        """按生命周期阶段回收直接子进程并收敛日志管道。

        参数:
            process: 已启动或尝试启动的直接子进程。
            process_group: 单次受保护进程组收敛任务的状态。
            log_pipes: 执行器显式持有的 stdout/stderr 管道。
            tasks: 本次 stdout/stderr 排空任务。

        返回:
            去重后的清理错误码；所有清理步骤成功时返回空元组。

        异常:
            本方法消化清理异常并转换为错误码，不掩盖调用方原异常。
        """
        issues: list[ScriptCleanupErrorCode] = []
        group_issue = await self._await_process_group_convergence(process_group)
        if group_issue is not None:
            issues.append(group_issue)
        if process.returncode is None:
            if group_issue is not None:
                direct_kill_issue = self._kill_direct_process(process)
                if direct_kill_issue is not None:
                    issues.append(direct_kill_issue)
            wait_issue = await self._wait_direct_process_bounded(process)
            if wait_issue is not None and process.returncode is None:
                direct_kill_issue = self._kill_direct_process(process)
                if direct_kill_issue is not None:
                    issues.append(direct_kill_issue)
                wait_issue = await self._wait_direct_process_bounded(process)
            if wait_issue is not None:
                issues.append(wait_issue)
        if process.returncode is None and process_group is not None:
            process_group.unreaped = True
        if log_pipes is not None:
            pipe_issue = await self._settle_log_pipes(log_pipes, tasks)
            if pipe_issue is not None:
                issues.append(pipe_issue)
        return tuple(dict.fromkeys(issues))

    @staticmethod
    def _kill_direct_process(
        process: asyncio.subprocess.Process,
    ) -> ScriptCleanupErrorCode | None:
        """使用当前 asyncio 子进程对象回退杀直接子进程。

        参数:
            process: 已启动且仍由当前事件循环持有的直接子进程对象。

        返回:
            进程已不存在时返回 ``None``；发送失败返回稳定清理错误码。

        异常:
            本方法消化进程已经退出或系统信号错误。
        """
        if process.returncode is not None:
            return None
        try:
            process.kill()
        except ProcessLookupError:
            return None
        except OSError:
            return ScriptCleanupErrorCode.PROCESS_WAIT_FAILED
        return None

    async def _wait_direct_process_bounded(
        self,
        process: asyncio.subprocess.Process,
    ) -> ScriptCleanupErrorCode | None:
        """在独立清理时限内等待直接子进程回收。

        参数:
            process: 当前执行持有的直接子进程。

        返回:
            已回收时返回 ``None``，超时或等待失败返回 ``PROCESS_WAIT_FAILED``。

        异常:
            本方法消化等待错误；清理任务自身取消时继续传播取消。
        """
        if process.returncode is not None:
            return None
        wait_task = asyncio.create_task(
            process.wait(),
            name="athena-script-direct-process-wait",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task),
                timeout=_DIRECT_PROCESS_CLEANUP_SECONDS,
            )
        except TimeoutError:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            return ScriptCleanupErrorCode.PROCESS_WAIT_FAILED
        except OSError:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            return ScriptCleanupErrorCode.PROCESS_WAIT_FAILED
        return None

    def _start_process_group_convergence(
        self,
        process_group: _ProcessGroupConvergence,
    ) -> asyncio.Task[ScriptCleanupErrorCode | None]:
        """创建或复用唯一受保护的进程组收敛任务。

        参数:
            process_group: 本生命周期记录 PGID、信号和收敛状态的共享对象。

        返回:
            已存在或刚创建的唯一收敛任务。

        异常:
            任务创建不主动抛出业务异常。
        """
        if process_group.task is None:
            process_group.task = asyncio.create_task(
                self._converge_process_group(process_group),
                name="athena-script-process-group-convergence",
            )
        return process_group.task

    async def _await_process_group_convergence(
        self,
        process_group: _ProcessGroupConvergence | None,
    ) -> ScriptCleanupErrorCode | None:
        """受保护地等待唯一收敛任务，外部取消不中止且不重建该任务。

        参数:
            process_group: 当前执行的进程组状态；尚未启动进程时为 ``None``。

        返回:
            收敛成功返回 ``None``，信号或确认失败返回稳定清理错误码。

        异常:
            外部取消在该任务完成后按原取消语义重新抛出。
        """
        if process_group is None:
            return None
        task = self._start_process_group_convergence(process_group)
        cancelled_error: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelled_error = error
        try:
            issue = task.result()
        except asyncio.CancelledError as error:
            cancelled_error = error
            issue = ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED
        except Exception:
            issue = ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED
        if issue is not None:
            process_group.unreaped = True
        if cancelled_error is not None:
            raise cancelled_error
        return issue

    async def _converge_process_group(
        self,
        process_group: _ProcessGroupConvergence,
    ) -> ScriptCleanupErrorCode | None:
        """每个生命周期只发送一次 SIGKILL，并分别记录发送和收敛状态。

        参数:
            process_group: 共享的 PGID、信号和收敛状态。

        返回:
            组已不存在或已收敛时返回 ``None``；发送或确认失败返回稳定清理错误码。

        异常:
            本方法消化 ``ProcessLookupError`` 和系统信号错误。
        """
        process_group_id = process_group.process_group_id
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            process_group.converged = True
            return None
        except OSError:
            process_group.unreaped = True
            return ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED
        process_group.signal_sent = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PROCESS_GROUP_EXIT_SECONDS
        while self._process_group_has_live_members(process_group_id):
            if loop.time() >= deadline:
                process_group.unreaped = True
                return ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED
            await asyncio.sleep(0.01)
        process_group.converged = True
        return None

    @staticmethod
    def _process_group_has_live_members(process_group_id: int) -> bool:
        """检查进程组是否仍有非僵尸成员。

        参数:
            process_group_id: 启动时记录的独立进程组 ID。

        返回:
            仍有存活成员或权限无法排除其存在时返回 ``True``。

        异常:
            本方法把信号检查异常转换为存在性结论。
        """
        proc_root = Path("/proc")
        if sys.platform.startswith("linux") and proc_root.is_dir():
            try:
                for entry in proc_root.iterdir():
                    if not entry.name.isdecimal():
                        continue
                    try:
                        process_stat = (entry / "stat").read_text(encoding="utf-8")
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    parsed = PythonScriptExecutor._parse_linux_process_stat(process_stat)
                    if parsed is None or parsed[1] != process_group_id:
                        continue
                    if parsed[0] != "Z":
                        return True
                return False
            except OSError:
                pass
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _parse_linux_process_stat(value: str) -> tuple[str, int] | None:
        """从 Linux ``/proc/<pid>/stat`` 提取进程状态和进程组。

        参数:
            value: 单行 stat 内容，第二字段的命令名可以包含空格和括号。

        返回:
            ``(state, pgrp)``；内容损坏或不是当前格式时返回 ``None``。

        异常:
            本方法不主动抛出业务异常。
        """
        closing_parenthesis = value.rfind(")")
        if closing_parenthesis < 0 or closing_parenthesis + 2 >= len(value):
            return None
        fields = value[closing_parenthesis + 2 :].split()
        if len(fields) < 3:
            return None
        try:
            process_group_id = int(fields[2])
        except ValueError:
            return None
        return fields[0], process_group_id

    async def _stage_manifest(
        self,
        working_directory: Path,
        output_directory: Path,
    ) -> tuple[Iterable[ScriptManifestOutput], ScriptExecutionErrorCode | None]:
        """在后台线程逐项暂停清单并通过 SQLite 建立磁盘后备索引。

        参数:
            working_directory: 本次调用的独占工作目录。
            output_directory: 进程结束后重新确认的输出目录。

        返回:
            可重复惰性遍历的输出声明及错误码；失败时输出声明为空。

        异常:
            外部取消会停止后台增量扫描、等待线程退出并原样传播取消。
        """
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
        if manifest_stat.st_nlink != 1:
            return (), ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED

        database_path = working_directory / _MANIFEST_STAGE_DATABASE
        try:
            database_path.unlink(missing_ok=True)
        except OSError:
            return (), ScriptExecutionErrorCode.MANIFEST_INVALID
        cancel_requested = threading.Event()
        worker_task = asyncio.create_task(
            asyncio.to_thread(
                self._stage_manifest_worker,
                manifest_path,
                output_directory,
                database_path,
                cancel_requested,
            ),
            name="athena-script-manifest-stager",
        )
        try:
            error_code = await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            cancel_requested.set()
            while not worker_task.done():
                try:
                    await asyncio.shield(worker_task)
                except asyncio.CancelledError:
                    continue
            raise
        if error_code is not None:
            return (), error_code
        return _SqliteManifestOutputs(database_path), None

    def _stage_manifest_worker(
        self,
        manifest_path: Path,
        output_directory: Path,
        database_path: Path,
        cancel_requested: threading.Event,
    ) -> ScriptExecutionErrorCode | None:
        """在线程中流式解析、逐文件校验并写入唯一路径索引。

        参数:
            manifest_path: 已确认无链接的 ``manifest.json``。
            output_directory: 本次脚本输出目录。
            database_path: 工作目录内的 SQLite 暂存数据库。
            cancel_requested: 主事件循环取消时设置的跨线程停止标记。

        返回:
            成功时返回 ``None``，否则返回可安全持久化的清单错误码。

        异常:
            本方法把清单、文件和暂存数据库问题转换为错误码，不向线程外抛出。
        """
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_path)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute(
                """
                CREATE TABLE outputs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL
                )
                """
            )
            with manifest_path.open(
                "r",
                encoding="utf-8",
                errors="strict",
                newline="",
            ) as stream:
                reader = _ManifestReader(stream, cancel_requested.is_set)
                for path, label in reader.iter_outputs():
                    if cancel_requested.is_set():
                        raise _ManifestStageCancelled()
                    normalized = self._normalize_manifest_path(path)
                    if normalized is None:
                        return ScriptExecutionErrorCode.OUTPUT_PATH_INVALID
                    if not self._valid_manifest_label(label):
                        return ScriptExecutionErrorCode.MANIFEST_INVALID

                    source_path = output_directory.joinpath(*PurePosixPath(normalized).parts)
                    try:
                        source_stat = source_path.lstat()
                    except FileNotFoundError:
                        return ScriptExecutionErrorCode.OUTPUT_MISSING
                    except OSError:
                        return ScriptExecutionErrorCode.OUTPUT_PATH_INVALID
                    if stat.S_ISLNK(source_stat.st_mode):
                        return ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
                    if not stat.S_ISREG(source_stat.st_mode):
                        return ScriptExecutionErrorCode.OUTPUT_NOT_REGULAR
                    if source_stat.st_nlink != 1:
                        return ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
                    try:
                        self._reject_existing_symlink_components(
                            output_directory,
                            source_path,
                        )
                    except ScriptExecutionConfigurationError:
                        return ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED
                    try:
                        connection.execute(
                            """
                            INSERT INTO outputs(path, label, source_path, size_bytes)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                normalized,
                                label,
                                str(source_path),
                                source_stat.st_size,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        return ScriptExecutionErrorCode.OUTPUT_DUPLICATE
            connection.commit()
            return None
        except _ManifestStageCancelled:
            if connection is not None:
                connection.rollback()
            return None
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            _ManifestInvalid,
            sqlite3.Error,
        ):
            if connection is not None:
                connection.rollback()
            return ScriptExecutionErrorCode.MANIFEST_INVALID
        finally:
            if connection is not None:
                connection.close()

    def _require_regular_contained_file(
        self,
        working_root: Path,
        candidate: Path,
        *,
        error_code: str,
    ) -> None:
        """确认路径位于工作目录内且为无链接普通文件。

        参数:
            working_root: 已归一化工作目录。
            candidate: 待校验的绝对或工作目录相对路径。
            error_code: 校验失败时写入配置异常的稳定错误码。

        返回:
            无返回值。

        异常:
            路径越界、链接、缺失或不是普通文件时抛出
            ``ScriptExecutionConfigurationError``。
        """
        resolved_candidate = candidate if candidate.is_absolute() else working_root / candidate
        try:
            resolved_candidate.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError(error_code) from error
        try:
            self._reject_existing_symlink_components(working_root, resolved_candidate)
            file_stat = resolved_candidate.lstat()
        except (OSError, ScriptExecutionConfigurationError) as error:
            if isinstance(error, ScriptExecutionConfigurationError):
                raise
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
        """逐段拒绝工作目录下目标路径中的符号链接。

        参数:
            working_root: 已归一化工作目录。
            candidate: 工作目录内待检查路径。

        返回:
            无返回值。

        异常:
            路径越界或任一已存在组件为链接时抛出
            ``ScriptExecutionConfigurationError``。
        """
        try:
            relative = candidate.relative_to(working_root)
        except ValueError as error:
            raise ScriptExecutionConfigurationError(
                "script_path_outside_working_directory"
            ) from error
        current = working_root
        for part in relative.parts:
            current = current / part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ScriptExecutionConfigurationError("script_path_invalid") from error
            if stat.S_ISLNK(current_stat.st_mode):
                raise ScriptExecutionConfigurationError("script_path_link_not_allowed")

    def _remove_working_directory(self, working_directory: Path) -> ScriptCleanupErrorCode | None:
        """删除执行器独占工作目录且不跟随目录链接。

        参数:
            working_directory: 已校验的本次调用根目录。

        返回:
            删除成功的 ``None`` 或稳定工作目录清理错误码。

        异常:
            本方法消化删除异常并转换为清理错误码。
        """
        try:
            shutil.rmtree(working_directory)
        except FileNotFoundError:
            return None
        except OSError:
            return ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED
        return None

    def _close_read_pipes(self, log_pipes: _LogPipeSet) -> bool:
        """关闭执行器显式持有的 stdout/stderr 异步读端。

        参数:
            log_pipes: 当前执行的两路日志管道。

        返回:
            全部现有读管道关闭成功时 ``True``，否则 ``False``。

        异常:
            本方法把关闭异常转换为 ``False``，重复关闭保持幂等。
        """
        succeeded = True
        for transport in (log_pipes.stdout_transport, log_pipes.stderr_transport):
            try:
                transport.close()
            except OSError:
                succeeded = False
        return succeeded

    @staticmethod
    def _normalize_manifest_path(path: str) -> str | None:
        """规范化清单输出路径并拒绝控制文件、反斜杠和越界段。

        参数:
            path: 清单中的 POSIX 相对文件路径。

        返回:
            规范化路径；非法时返回 ``None``。

        异常:
            本方法不主动抛出业务异常。
        """
        if (
            not path
            or "\x00" in path
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            return None
        relative = PurePosixPath(path)
        if (
            relative.is_absolute()
            or relative.as_posix() != path
            or path == "manifest.json"
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            return None
        return relative.as_posix()

    @staticmethod
    def _valid_manifest_label(label: str) -> bool:
        """检查展示标签为有界纯文本。

        参数:
            label: 清单中的交付文件展示标签。

        返回:
            标签非空、长度合法且不含控制字符时返回 ``True``。

        异常:
            本方法不主动抛出业务异常。
        """
        return (
            bool(label)
            and len(label) <= _MANIFEST_LABEL_MAX_CHARACTERS
            and all(ord(character) >= 32 and ord(character) != 127 for character in label)
        )

    @staticmethod
    def _append_cleanup_issue(
        result: ScriptExecutionResult,
        issue: ScriptCleanupErrorCode | None,
    ) -> None:
        """向结果追加一个不重复的清理错误码。

        参数:
            result: 当前执行结果。
            issue: 新发现的清理错误；``None`` 表示无需记录。

        返回:
            无返回值。

        异常:
            本方法不主动抛出业务异常。
        """
        if issue is not None and issue not in result.cleanup_error_codes:
            result.cleanup_error_codes.append(issue)


__all__ = ["MAX_SCRIPT_LOG_BYTES", "PythonScriptExecutor"]
