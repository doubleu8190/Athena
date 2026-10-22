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
    CANCELLING = "cancelling"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunCancellationResult:
    """停止请求的当前状态，以及本次调用是否首次推进了取消。"""

    status: RunStatus
    changed: bool
    external_effect_status: str = "unknown"


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
class RootWaitCheckpoint:
    """由 Root Graph 中断产生、用于登记恢复位置的身份信息。"""

    run_id: str
    plan_id: str
    round_id: str
    plan_version: int
    interrupt_id: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        identity = (
            self.run_id,
            self.plan_id,
            self.round_id,
            self.interrupt_id,
            self.checkpoint_id,
        )
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            raise ValueError("Root wait identity fields must not be empty")
        if type(self.plan_version) is not int or self.plan_version < 1:
            raise ValueError("plan_version must be a positive integer")


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    """执行器返回的结果；等待时必须携带经过验证的 Root checkpoint。"""

    result: dict[str, object] | None = None
    error: dict[str, object] | None = None
    root_wait: RootWaitCheckpoint | None = None

    @property
    def waiting(self) -> bool:
        """Whether the executor suspended at a durable Root checkpoint."""
        return self.root_wait is not None


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
