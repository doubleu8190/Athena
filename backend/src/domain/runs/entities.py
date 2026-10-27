"""运行命令、生命周期和查询所需的领域类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from domain.leases import LeaseGrant

from .payloads import RunCommandPayload, RunResumePayload, RunStartPayload


class CommandType(StrEnum):
    """内部 Run 执行器的调用类型，不作为业务状态持久化。"""

    RUN_START = "run.start"
    RUN_RESUME = "run.resume"


class RunStatus(StrEnum):
    """用户消息对应 Run 的统一生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunCancellationResult:
    """停止请求的当前状态，以及本次调用是否首次推进了取消。"""

    status: RunStatus
    changed: bool
    external_effect_status: str = "unknown"
    run_id: str | None = None
    no_op: bool = False


@dataclass(frozen=True, slots=True)
class RunCommand:
    """一次 Root start/resume 调用，不代表独立的持久化命令。"""

    command_id: str
    command_type: CommandType
    session_id: str
    run_id: str | None
    payload: RunCommandPayload
    issued_at: datetime
    schema_version: int = 1
    lease: LeaseGrant | None = None

    def __post_init__(self) -> None:
        """Reject commands whose concrete payload does not match their type."""
        expected_payload = _payload_type_for(self.command_type)
        if not isinstance(self.payload, expected_payload):
            raise ValueError(
                f"{self.command_type.value} requires {expected_payload.__name__}"
            )
        if self.run_id is not None and (
            self.lease is None
            or self.lease.key.resource_type != "root_execution"
            or self.lease.key.resource_id != self.run_id
        ):
            raise ValueError("run_command_requires_matching_root_lease")


def _payload_type_for(command_type: CommandType) -> type[RunCommandPayload]:
    """Return the validated payload type for an internal Run invocation."""
    payload_types: dict[CommandType, type[RunCommandPayload]] = {
        CommandType.RUN_START: RunStartPayload,
        CommandType.RUN_RESUME: RunResumePayload,
    }
    try:
        return payload_types[command_type]
    except KeyError as error:
        raise ValueError(f"unsupported_run_command_type:{command_type}") from error


@dataclass(frozen=True, slots=True)
class RunEnqueueResult:
    """Run 与源消息写入后的稳定响应。"""

    session_id: str
    run_id: str
    message_id: str
    sequence: int
    status: RunStatus | str
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
    message_id: str
    sequence: int
    status: RunStatus | str
    root_thread_id: str
    error: Any = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    started_at: datetime | str | None = None
    title: str = ""
