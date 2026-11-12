"""Python 技能脚本执行的显式领域契约。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

SCRIPT_PROCESS_DIAGNOSTIC_FILENAME = ".athena-process-diagnostic.json"

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class ScriptProcessDiagnostic(BaseModel):
    """只供人工排查、不能证明进程存活的本地启动记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    process_id: int = Field(gt=0)
    process_group_id: int = Field(gt=0)
    recorded_at: datetime
    working_directory: str = Field(min_length=1, max_length=4096)


class ScriptExecutionErrorCode(StrEnum):
    """脚本启动、进程退出或输出清单失败时使用的稳定错误码。"""

    PROCESS_START_FAILED = "script_process_start_failed"
    PROCESS_FAILED = "script_process_failed"
    PROCESS_CLEANUP_FAILED = "script_process_cleanup_failed"
    EXIT_NONZERO = "script_exit_nonzero"
    MANIFEST_MISSING = "script_manifest_missing"
    MANIFEST_INVALID = "script_manifest_invalid"
    OUTPUT_PATH_INVALID = "script_output_path_invalid"
    OUTPUT_DUPLICATE = "script_output_duplicate"
    OUTPUT_MISSING = "script_output_missing"
    OUTPUT_LINK_NOT_ALLOWED = "script_output_link_not_allowed"
    OUTPUT_NOT_REGULAR = "script_output_not_regular"


class ScriptCleanupErrorCode(StrEnum):
    """进程组、日志管道或工作目录未能确认清理时的稳定错误码。"""

    PROCESS_WAIT_FAILED = "script_process_wait_failed"
    PROCESS_GROUP_CLEANUP_FAILED = "script_process_group_cleanup_failed"
    LOG_PIPE_CLEANUP_FAILED = "script_log_pipe_cleanup_failed"
    WORKING_DIRECTORY_CLEANUP_FAILED = "script_working_directory_cleanup_failed"


class ScriptExecutionConfigurationError(ValueError):
    """执行请求在执行前未通过平台路径或输入约束。"""

    def __init__(
        self,
        code: str = "script_execution_request_invalid",
        cleanup_error_codes: tuple[ScriptCleanupErrorCode, ...] = (),
    ) -> None:
        """保存可安全映射给调用层的稳定错误码。

        参数:
            code: 不含路径、参数正文或底层异常详情的稳定错误码。
            cleanup_error_codes: 准备失败后目录清理未成功时的稳定错误码。

        返回:
            无返回值；实例自身携带该错误码。

        异常:
            构造函数不主动抛出业务异常。
        """
        super().__init__(code)
        self.code = code
        self.cleanup_error_codes = cleanup_error_codes

    @property
    def cleanup_error_code(self) -> str | None:
        """返回准备失败清理阶段的首个错误码。

        参数:
            无参数。

        返回:
            首个清理错误码字符串；清理成功或尚未尝试时返回 ``None``。

        异常:
            属性访问不主动抛出业务异常。
        """
        if not self.cleanup_error_codes:
            return None
        return self.cleanup_error_codes[0].value


class ScriptExecutionCancelled(asyncio.CancelledError):
    """保留异步取消语义，同时公开取消期间最终清理结论。"""

    def __init__(
        self,
        cleanup_error_codes: tuple[ScriptCleanupErrorCode, ...] = (),
        process_group_id: int | None = None,
        process_reaped: bool | None = None,
    ) -> None:
        """创建携带清理错误码的取消异常。

        参数:
            cleanup_error_codes: 进程组、管道或工作目录未确认清理的错误码。
            process_group_id: 启动时记录的 PGID，也是未回收时的恢复定位信息；后续必须
                结合宿主系统工具人工确认，不能只凭该数值再次发信号。
            process_reaped: 直接子进程是否已确认回收；无法确认时为 ``False``。

        返回:
            无返回值。

        异常:
            构造函数不主动抛出业务异常。
        """
        super().__init__("script_execution_cancelled")
        self.cleanup_error_codes = cleanup_error_codes
        self.process_group_id = process_group_id
        self.process_reaped = process_reaped

    @property
    def cleanup_error_code(self) -> str | None:
        """返回取消清理阶段的首个错误码。

        参数:
            无参数。

        返回:
            首个清理错误码字符串；清理成功时返回 ``None``。

        异常:
            属性访问不主动抛出业务异常。
        """
        if not self.cleanup_error_codes:
            return None
        return self.cleanup_error_codes[0].value


@dataclass(frozen=True, slots=True)
class ScriptExecutionRequest:
    """一次已准备目录中的登记脚本执行请求。

    ``working_directory`` 和 ``output_directory`` 必须是平台为该调用创建的绝对目录；
    ``script_path`` 是相对工作目录的登记入口路径；``input_files`` 中每个路径必须是
    已复制进工作目录的普通文件。
    """

    working_directory: Path
    script_path: str
    parameters: Mapping[str, JsonValue]
    input_files: Mapping[str, Path]
    output_directory: Path
    run_id: str
    tool_call_id: str
    worker_instance_id: str


@dataclass(frozen=True, slots=True)
class ScriptManifestOutput:
    """清单中一个已校验的普通文件，不包含文件正文。"""

    path: str
    label: str
    source_path: Path
    size_bytes: int


@dataclass(slots=True)
class ScriptExecutionResult:
    """脚本进程、输出声明、有界日志及最终清理问题。

    ``outputs`` 是适配器拥有的可重复惰性迭代器，具体实现可以在工作目录内使用磁盘后备
    存储，调用方必须逐项上传其中的 ``source_path``。上下文退出后工作目录会被删除，
    届时 ``outputs`` 不可再读取，``cleanup_error_codes`` 才包含最终清理结论。
    """

    exit_code: int | None
    error_code: ScriptExecutionErrorCode | None
    outputs: Iterable[ScriptManifestOutput]
    stdout: bytes
    stderr: bytes
    log_truncated: bool
    process_group_id: int | None
    cleanup_error_codes: list[ScriptCleanupErrorCode] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """返回退出码为零且输出清单和全部声明文件均有效的结论。

        参数:
            无参数。

        返回:
            ``True`` 表示脚本及清单协议成功；否则返回 ``False``。

        异常:
            属性访问不主动抛出业务异常。
        """
        return self.exit_code == 0 and self.error_code is None

    @property
    def cleanup_error_code(self) -> str | None:
        """返回首个清理错误码，供现有单字段持久化模型使用。

        参数:
            无参数。

        返回:
            首个清理错误码字符串；没有清理问题时返回 ``None``。

        异常:
            属性访问不主动抛出业务异常。
        """
        if not self.cleanup_error_codes:
            return None
        return self.cleanup_error_codes[0].value


class ScriptExecutionPort(Protocol):
    """执行已登记 Python 脚本并管理进程、管道和工作目录生命周期。"""

    def execute(
        self, request: ScriptExecutionRequest
    ) -> AbstractAsyncContextManager[ScriptExecutionResult]:
        """启动一次执行会话。

        参数:
            request: 平台已准备的独立工作目录、登记脚本、JSON 参数、输入副本映射和
                输出目录。

        返回:
            异步上下文管理器。进入后等待脚本和进程组收敛、校验清单并产出结果；
            调用方必须在退出上下文前完成待发布文件上传，退出时执行器删除工作目录。

        异常:
            请求路径或输入不满足协议时抛出 ``ScriptExecutionConfigurationError``；
            外部 ``asyncio.CancelledError`` 会先触发进程组和日志清理再原样传播。
        """


__all__ = [
    "JsonScalar",
    "JsonValue",
    "SCRIPT_PROCESS_DIAGNOSTIC_FILENAME",
    "ScriptCleanupErrorCode",
    "ScriptExecutionConfigurationError",
    "ScriptExecutionCancelled",
    "ScriptExecutionErrorCode",
    "ScriptExecutionPort",
    "ScriptExecutionRequest",
    "ScriptExecutionResult",
    "ScriptManifestOutput",
    "ScriptProcessDiagnostic",
]
