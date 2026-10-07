"""运行查询所需的无框架领域类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class CommandType(StrEnum):
    """可由 HTTP 或后台 Worker 提交的运行控制命令。"""

    MESSAGE_SUBMIT = "message.submit"
    RUN_START = "run.start"
    RUN_CANCEL = "run.cancel"
    RUN_PAUSE = "run.pause"
    RUN_RESUME = "run.resume"
    APPROVAL_RESOLVE = "approval.resolve"


class CommandStatus(StrEnum):
    """命令在持久化队列中的生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunStatus(StrEnum):
    """运行状态的稳定传输值。"""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunCommand:
    """与传输技术无关的命令信封。"""

    command_id: str
    command_type: CommandType
    session_id: str
    run_id: str | None
    payload: Mapping[str, Any]
    issued_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CommandStatusRecord:
    """命令状态查询返回的只读领域记录。"""

    command_id: str
    session_id: str
    run_id: str | None
    command_type: CommandType | str
    status: CommandStatus | str
    result: Any = None
    error: Any = None


@dataclass(frozen=True, slots=True)
class CommandEnqueueResult:
    """命令入队后的稳定结果，包含幂等复用信息。"""

    command_id: str
    run_id: str | None
    message_id: str | None
    attachment_ids: tuple[str, ...]
    status: CommandStatus | str
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    """执行器返回的稳定结果。"""

    result: Any = None
    error: Any = None
    waiting: bool = False


@dataclass(frozen=True, slots=True)
class RunSummary:
    """会话运行的只读摘要。"""

    run_id: str
    session_id: str
    status: RunStatus | str
    root_thread_id: str
    error: Any = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
